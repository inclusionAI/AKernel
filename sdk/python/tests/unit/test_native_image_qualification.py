"""Native qualification must reject existing data and changed resource identity."""

import gzip
import importlib.util
import io
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "native_image_qualification",
    Path(__file__).resolve().parents[4] / "deploy/tests/managed_images/native.py",
)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class NativeQualificationGuardTest(unittest.TestCase):
    def test_seed_layer_media_type_preserves_compressed_docker_archive_bytes(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.addfile(tarfile.TarInfo("fixture"))
        raw = stream.getvalue()
        media = "application/vnd.oci.image.layer.v1.tar"
        self.assertEqual(native.layer_media_type(raw), media)
        self.assertEqual(native.layer_media_type(gzip.compress(raw)), media + "+gzip")

    def fixture(self, root):
        return native.Fixture(
            Namespace(
                output=root / "evidence",
                data_root=root / "data",
                disk_uuid="owned-disk",
            )
        )

    def test_existing_data_is_preserved_before_any_filesystem_action(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            fixture.data.mkdir(parents=True)
            marker = fixture.data / "historical"
            marker.write_text("preserve")
            with patch.object(native, "command", return_value="owned-disk") as commands:
                with self.assertRaisesRegex(RuntimeError, "fresh and empty"):
                    fixture.setup()
            self.assertEqual(marker.read_text(), "preserve")
            self.assertEqual([c.args[0] for c in commands.call_args_list], ["findmnt"])

    def test_reassigned_loop_is_never_detached_by_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            fixture.loop = "/dev/loop999"
            with patch.object(
                native, "command", return_value="/unrelated/backing"
            ) as commands:
                errors = fixture.cleanup()
            self.assertTrue(errors)
            self.assertEqual(fixture.loop, "/dev/loop999")
            self.assertEqual(len(commands.call_args_list), 1)
            self.assertNotIn("--detach", commands.call_args.args)

    def test_foreign_container_label_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            with patch.object(
                native,
                "command",
                return_value='[{"Config":{"Labels":{"akernel.qualification":"other-run"}}}]',
            ):
                with self.assertRaisesRegex(RuntimeError, "identity changed"):
                    fixture.inspect("foreign")
