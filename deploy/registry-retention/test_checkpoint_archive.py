"""Safety of checkpoint image archives across capture, restore and GC."""

import base64
import hashlib
import io
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from checkpoint_archive import MANIFEST_TYPES, NYDUS_BLOB, NYDUS_BOOTSTRAP, RECEIPT, REGISTRY_USER
from retention import Retention, server_handler


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ArchiveTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        runner = self.root / "runner-token"
        runner.write_text("runner-token-for-tests")
        worker = self.root / "worker-token"
        worker.write_text("worker-token-for-tests")
        self.registry_token = self.root / "registry-token"
        self.registry_token.write_text("registry-token-for-tests")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        (self.artifacts.parent / "store-scope").write_text(str(uuid.uuid4()))
        self.config = {
            "adx_origin": "https://adx.example.test",
            "runner_token_file": str(runner),
            "adx_token_file": str(runner),
            "initial_slot": "a",
            "auto_delete": True,
            "registries": {
                slot: {"origin": f"https://registry-{slot}.example.test", "profile": f"harbor-ephemeral-{slot}"}
                for slot in ("a", "b")
            },
            "checkpoint_archive": {
                "mount": str(self.root),
                "checkpoint_artifacts": str(self.artifacts),
                "root": str(self.root / "archive"),
                "volume_identity": "test-volume",
                "registry_origin": "https://archive.example.test",
                "worker_token_file": str(worker),
                "registry_token_file": str(self.registry_token),
                "nydus_image": "/bin/true",
                "auto_delete": True,
            },
        }
        mount = patch("checkpoint_archive.CheckpointArchive._check_volume")
        self.mount = mount.start()
        self.addCleanup(mount.stop)
        self.retention = Retention(self.config, self.root / "retention.sqlite")
        self.archive = self.retention.archive
        self.data = b"0123456789"
        self.data_digest = digest(self.data)
        bootstrap = b"fake bootstrap envelope"
        config_blob = b"{}"
        self.raw_blobs = {digest(data): data for data in (bootstrap, config_blob, self.data)}

        def layer(content, media, **extra):
            return {"digest": digest(content), "size": len(content), "mediaType": media, **extra}

        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPES[1],
            "config": layer(config_blob, "application/vnd.oci.image.config.v1+json"),
            "layers": [
                layer(self.data, NYDUS_BLOB),
                layer(
                    bootstrap, "application/vnd.oci.image.layer.v1.tar+gzip",
                    annotations={NYDUS_BOOTSTRAP: "true"},
                ),
            ],
        }
        self.manifest = json.dumps(manifest).encode()
        self.reference = ("registry-a.example.test/akernel-ephemeral/" + "d" * 32
                          + "/service-0@" + digest(self.manifest))
        publication = self.retention.begin(1)
        self.publication = publication["id"]
        self.retention.plan(self.publication, self.reference.split("@")[0] + ":" + publication["tag"])
        with patch.object(self.retention.registries["a"], "resolve", return_value=self.reference):
            self.retention.commit(self.publication, self.reference)
        with self.retention.database_session(write=True) as db:
            db.execute("UPDATE publications SET expires_at=0 WHERE id=?", (self.publication,))
        registry = self.retention.registries["a"]
        for mock in (
            patch.object(registry, "call", return_value=(200, {"Content-Type": MANIFEST_TYPES[1]}, self.manifest)),
            patch.object(registry, "open_blob", side_effect=lambda _, value: io.BytesIO(self.raw_blobs[value])),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        inspector = patch("checkpoint_archive.inspect_bootstrap", return_value={self.data_digest})
        self.inspect = inspector.start()
        self.addCleanup(inspector.stop)

    def artifact(self):
        artifact = str(uuid.uuid4())
        (self.artifacts / artifact).mkdir()
        return artifact

    def test_verified_graph_deduplicates_and_last_runtime_protects_objects(self):
        first, second = self.artifact(), self.artifact()
        source, restored = uuid.uuid4().hex, uuid.uuid4().hex
        self.archive.capture(first, self.reference, source)
        self.archive.capture(second, self.reference, source)
        self.inspect.assert_called_once()
        self.assertEqual(set(path.name for path in self.archive.objects.iterdir()),
                         {*self.raw_blobs, digest(self.manifest)})
        restore = self.archive.restore(first, self.reference, restored)
        self.assertTrue(restore["reference"].endswith("@" + digest(self.manifest)))
        for artifact in (first, second):
            (self.artifacts / artifact / RECEIPT).unlink()
            (self.artifacts / artifact).rmdir()
        self.archive.release(source)
        self.archive.sweep()
        self.assertTrue((self.archive.objects / self.data_digest).exists())
        self.archive.release(restored)
        self.archive.sweep()
        self.assertEqual(list(self.archive.objects.iterdir()), [])

    def test_missing_nydus_blob_fails_before_ready(self):
        self.inspect.return_value = {"sha256:" + "f" * 64}
        artifact = self.artifact()
        with self.assertRaisesRegex(ValueError, "Nydus bootstrap references"):
            self.archive.capture(artifact, self.reference, uuid.uuid4().hex)
        self.assertFalse((self.artifacts / artifact / RECEIPT).exists())
        self.assertEqual([row["id"] for row in self.retention.candidates()], [self.publication])
        self.assertEqual(list(self.archive.objects.iterdir()), [])

    def test_failed_copy_fences_source_gc_and_late_runtime_link(self):
        artifact, lease = self.artifact(), uuid.uuid4().hex
        copy_started, copy_may_finish = threading.Event(), threading.Event()
        real_copy = self.archive._copy_graph

        def delayed_copy(*args):
            copy_started.set()
            if not copy_may_finish.wait(5):
                raise RuntimeError("test failed to release the copy")
            return real_copy(*args)

        stale_candidate = self.retention.candidates()[0]
        registry = self.retention.registries["a"]
        with (patch.object(self.archive, "_copy_graph", delayed_copy),
              patch.object(registry, "worklist", return_value=[digest(self.manifest)]),
              patch.object(registry, "delete") as delete,
              ThreadPoolExecutor() as pool):
            future = pool.submit(self.archive.capture, artifact, self.reference, lease)
            try:
                self.assertTrue(copy_started.wait(5))
                self.assertEqual(self.retention.candidates(), [])
                self.retention.reclaim(stale_candidate)
                delete.assert_not_called()
                self.archive.release(lease)
            finally:
                copy_may_finish.set()
            with self.assertRaisesRegex(ValueError, "runtime lease has been released"):
                future.result(timeout=5)
        self.assertFalse((self.artifacts / artifact / RECEIPT).exists())

    def test_registry_range_and_private_operations(self):
        artifact, lease = self.artifact(), uuid.uuid4().hex
        self.archive.capture(artifact, self.reference, lease)
        with ThreadingHTTPServer(("127.0.0.1", 0), server_handler(self.retention)) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                blob = f"{url}/v2/akernel-checkpoint/{lease}/blobs/{self.data_digest}"
                with self.assertRaises(HTTPError) as anonymous:
                    urlopen(blob, timeout=2)
                self.assertEqual(anonymous.exception.code, 401)
                self.assertIn("Basic", anonymous.exception.headers["WWW-Authenticate"])
                anonymous.exception.close()
                auth = base64.b64encode(f"{REGISTRY_USER}:registry-token-for-tests".encode()).decode()
                with urlopen(Request(
                    blob, headers={"Range": "bytes=2-4", "Authorization": "Basic " + auth},
                ), timeout=2) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers["Content-Range"], "bytes 2-4/10")
                    self.assertEqual(response.read(), b"234")
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(Request(
                        url + "/v1/archive/release", data=json.dumps({"lease": lease}).encode(),
                        headers={"Authorization": "Bearer runner-token-for-tests"},
                    ), timeout=2)
                self.assertEqual(invalid.exception.code, 401)
                invalid.exception.close()
            finally:
                server.shutdown()
                worker.join()

    def test_restore_fails_before_leasing_a_missing_blob(self):
        artifact, source, restored = self.artifact(), uuid.uuid4().hex, uuid.uuid4().hex
        self.archive.capture(artifact, self.reference, source)
        (self.archive.objects / self.data_digest).unlink()
        with self.assertRaises(OSError):
            self.archive.restore(artifact, self.reference, restored)
        with self.retention.database_session() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM archive_runtime WHERE lease=?", (restored,)
            ).fetchone())

    def test_volume_failure_and_ledger_loss_preserve_the_archive(self):
        artifact, lease = self.artifact(), uuid.uuid4().hex
        self.archive.capture(artifact, self.reference, lease)
        self.archive.release(lease)
        receipt = self.artifacts / artifact / RECEIPT
        receipt.unlink()
        receipt.parent.rmdir()
        self.mount.side_effect = OSError("volume unavailable")
        with self.assertRaises(OSError):
            self.archive.sweep()
        self.assertTrue((self.archive.objects / self.data_digest).exists())
        self.mount.side_effect = None
        (self.artifacts.parent / "store-scope").write_text(str(uuid.uuid4()))
        with self.assertRaisesRegex(OSError, "scope does not match"):
            self.archive.sweep()
        self.assertTrue((self.archive.objects / self.data_digest).exists())
        with self.assertRaisesRegex(ValueError, "ledger is missing"):
            Retention(self.config, self.root / "different.sqlite")
