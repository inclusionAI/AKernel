"""Resolve a post-publication history error without replaying the native build."""

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "runtime_publication", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)

TAG = "build-" + "a" * 32
REFERENCE = "registry.test/poc/job/main:" + TAG
RAW = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": "sha256:" + "b" * 64},
        "layers": [],
    }
).encode()
DIGEST = "sha256:" + hashlib.sha256(RAW).hexdigest()
INSPECTION = {
    "Descriptor": {
        "digest": DIGEST,
        "size": len(RAW),
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"os": "linux", "architecture": "amd64"},
    },
    "Raw": base64.b64encode(RAW).decode(),
}
HISTORY = (
    b"ERROR: failed to solve: Internal: unknown blob sha256:"
    + b"c" * 64
    + b" in history\n"
)
DEADLINE = b"ERROR: failed to solve: Internal: context deadline exceeded\n"
PUSH = f"#19 pushing manifest for {REFERENCE}@{DIGEST} 0.2s done\n".encode()
PLAN = {
    "target": {"main": {"tags": [REFERENCE]}},
    "group": {"default": {"targets": ["main"]}},
}
PROFILE = {
    "registry": "registry.test",
    "prefix": "poc/job",
    "registry_insecure": True,
    "builder": "remote-test",
}


class PublicationResultTest(unittest.TestCase):
    def test_explicit_metadata_provenance_setting_is_preserved(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"BUILDX_METADATA_PROVENANCE": "max"}),
            patch.object(compose, "execute_bake") as solve,
        ):

            def completed(*_):
                self.assertEqual(os.environ["BUILDX_METADATA_PROVENANCE"], "max")
                (Path(root) / "metadata.json").write_text(
                    json.dumps({"main": {"containerimage.digest": DIGEST}})
                )

            solve.side_effect = completed
            self.assertEqual(self.call(root)["main"]["containerimage.digest"], DIGEST)

    def test_deadline_adopts_only_an_acknowledged_matching_published_output(self):
        error = subprocess.CalledProcessError(100, ["docker"], stderr=PUSH + DEADLINE)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error) as solve,
            patch.object(compose, "execute", return_value=json.dumps(INSPECTION)),
        ):
            result = self.call(root)
            self.assertEqual(result["main"]["containerimage.digest"], DIGEST)
            self.assertEqual(solve.call_count, 1)
            receipt = json.loads(
                (Path(root) / "metadata.json.publication.json").read_text()
            )
            self.assertEqual(
                receipt["failure_kind"], "acknowledged-publication-deadline"
            )
            self.assertFalse(receipt["native_build_replayed"])

    def test_deadline_without_a_complete_matching_push_acknowledgement_fails_closed(
        self,
    ):
        for log in [
            DEADLINE,
            PUSH.replace(b" done", b" pending") + DEADLINE,
            PUSH.replace(REFERENCE.encode(), b"registry.test/other:built") + DEADLINE,
        ]:
            error = subprocess.CalledProcessError(100, ["docker"], stderr=log)
            with (
                self.subTest(log=log),
                tempfile.TemporaryDirectory() as root,
                patch.object(compose, "execute_bake", side_effect=error),
                patch.object(compose, "execute") as inspect,
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.call(root)
                self.assertIs(caught.exception, error)
                inspect.assert_not_called()

    def test_deadline_preserves_failure_on_push_digest_mismatch(self):
        error = subprocess.CalledProcessError(
            100,
            ["docker"],
            stderr=PUSH.replace(DIGEST.encode(), ("sha256:" + "d" * 64).encode())
            + DEADLINE,
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error),
            patch.object(compose, "execute", return_value=json.dumps(INSPECTION)),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.call(root)
            self.assertIs(caught.exception, error)
            self.assertFalse((Path(root) / "metadata.json").exists())

    def test_deadline_partial_group_and_explicit_attestations_are_not_adopted(self):
        partial = {
            "target": {
                "main": {"tags": [REFERENCE]},
                "loadgen": {"tags": ["registry.test/poc/job/loadgen:" + TAG]},
            },
            "group": {"default": {"targets": ["main", "loadgen"]}},
        }
        attested = {
            "target": {"main": {"tags": [REFERENCE], "attest": ["type=provenance"]}},
            "group": {"default": {"targets": ["main"]}},
        }
        for plan in [partial, attested]:
            error = subprocess.CalledProcessError(
                100, ["docker"], stderr=PUSH + DEADLINE
            )
            with (
                self.subTest(plan=plan),
                tempfile.TemporaryDirectory() as root,
                patch.object(compose, "execute_bake", side_effect=error),
                patch.object(compose, "execute") as inspect,
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.call(root, plan)
                self.assertIs(caught.exception, error)
                inspect.assert_not_called()

    def test_deadline_does_not_adopt_signals_or_other_internal_failures(self):
        for code, log in [
            (1, PUSH + DEADLINE),
            (137, PUSH + DEADLINE),
            (100, PUSH + b"ERROR: failed to solve: Internal: other error\n"),
        ]:
            error = subprocess.CalledProcessError(code, ["docker"], stderr=log)
            with (
                self.subTest(code=code),
                tempfile.TemporaryDirectory() as root,
                patch.object(compose, "execute_bake", side_effect=error),
                patch.object(compose, "execute") as inspect,
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.call(root)
                self.assertIs(caught.exception, error)
                inspect.assert_not_called()

    def call(self, root, plan=PLAN, profile=PROFILE):
        metadata = Path(root) / "metadata.json"
        command = [
            "docker",
            "buildx",
            "bake",
            "--metadata-file",
            str(metadata),
            "default",
        ]
        return compose.bake_metadata(command, plan, metadata, profile)

    def test_history_failure_adopts_hash_verified_published_image_without_second_solve(
        self,
    ):
        error = subprocess.CalledProcessError(100, ["docker"], stderr=HISTORY)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error) as solve,
            patch.object(
                compose, "execute", return_value=json.dumps(INSPECTION)
            ) as inspect,
        ):
            result = self.call(root)
            self.assertEqual(result["main"]["containerimage.digest"], DIGEST)
            self.assertTrue(result["main"]["akernel.publication-reconciled"])
            self.assertEqual(solve.call_count, 1)
            self.assertEqual(
                inspect.call_args.args[0],
                ["docker", "manifest", "inspect", "--verbose", "--insecure", REFERENCE],
            )
            self.assertTrue((Path(root) / "metadata.json.publication.json").exists())

    def test_success_uses_fresh_native_metadata_without_registry_lookup(self):
        with tempfile.TemporaryDirectory() as root:
            metadata = Path(root) / "metadata.json"
            metadata.write_text(
                json.dumps({"main": {"containerimage.digest": "sha256:" + "f" * 64}})
            )

            def solve(*_):
                self.assertFalse(metadata.exists())
                metadata.write_text(
                    json.dumps({"main": {"containerimage.digest": DIGEST}})
                )

            with (
                patch.object(compose, "execute_bake", side_effect=solve),
                patch.object(compose, "execute") as inspect,
            ):
                self.assertEqual(
                    self.call(root)["main"]["containerimage.digest"], DIGEST
                )
            inspect.assert_not_called()

    def test_non_history_authorization_and_signal_failures_keep_original_error(self):
        for code, message in [
            (1, HISTORY),
            (100, b"ERROR: unauthorized\n"),
            (137, HISTORY),
            (100, HISTORY + b"ERROR: final error is a different failure\n"),
        ]:
            error = subprocess.CalledProcessError(code, ["docker"], stderr=message)
            with (
                self.subTest(code=code, message=message),
                tempfile.TemporaryDirectory() as root,
                patch.object(compose, "execute_bake", side_effect=error),
                patch.object(compose, "execute") as inspect,
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.call(root)
                self.assertIs(caught.exception, error)
                inspect.assert_not_called()

    def test_unknown_process_outcome_is_not_reconciled_or_replayed(self):
        error = OSError("unknown process outcome")
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error),
            patch.object(compose, "execute") as inspect,
        ):
            with self.assertRaises(OSError) as caught:
                self.call(root)
            self.assertIs(caught.exception, error)
            inspect.assert_not_called()

    def test_reused_mutable_tag_and_foreign_repository_cannot_authorize_recovery(self):
        for reference in [
            "registry.test/poc/job/main:built",
            "other.test/poc/job/main:" + TAG,
        ]:
            error = subprocess.CalledProcessError(100, ["docker"], stderr=HISTORY)
            plan = {
                "target": {"main": {"tags": [reference]}},
                "group": {"default": {"targets": ["main"]}},
            }
            with (
                self.subTest(reference=reference),
                tempfile.TemporaryDirectory() as root,
                patch.object(compose, "execute_bake", side_effect=error),
                patch.object(compose, "execute") as inspect,
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.call(root, plan)
                self.assertIs(caught.exception, error)
                inspect.assert_not_called()

    def test_invalid_registry_content_never_becomes_success(self):
        variants = [
            [],
            {**INSPECTION, "Raw": base64.b64encode(b"wrong bytes").decode()},
            {
                **INSPECTION,
                "Descriptor": {
                    **INSPECTION["Descriptor"],
                    "digest": "sha256:" + "d" * 64,
                },
            },
            {
                **INSPECTION,
                "Descriptor": {**INSPECTION["Descriptor"], "size": len(RAW) + 1},
            },
            {
                **INSPECTION,
                "Descriptor": {
                    **INSPECTION["Descriptor"],
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            },
        ]
        for value in variants:
            error = subprocess.CalledProcessError(100, ["docker"], stderr=HISTORY)
            with (
                self.subTest(value=value),
                tempfile.TemporaryDirectory() as root,
                patch.object(compose, "execute_bake", side_effect=error),
                patch.object(compose, "execute", return_value=json.dumps(value)),
            ):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.call(root)
                self.assertIs(caught.exception, error)
                self.assertFalse((Path(root) / "metadata.json").exists())

    def test_partial_compose_publication_preserves_failure_and_original_diagnostics(
        self,
    ):
        second = "registry.test/poc/job/loadgen:" + TAG
        plan = {
            "target": {"main": {"tags": [REFERENCE]}, "loadgen": {"tags": [second]}},
            "group": {"default": {"targets": ["main", "loadgen"]}},
        }
        error = subprocess.CalledProcessError(100, ["docker"], stderr=HISTORY)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error),
            patch.object(
                compose,
                "execute",
                side_effect=[json.dumps(INSPECTION), RuntimeError("manifest missing")],
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.call(root, plan)
            self.assertIs(caught.exception, error)
            self.assertEqual(caught.exception.stderr, HISTORY)
            self.assertFalse((Path(root) / "metadata.json").exists())

    def test_no_cache_does_not_replay_a_solve_during_publication_recovery(self):
        plan = {
            "target": {"main": {"tags": [REFERENCE], "no-cache": True}},
            "group": {"default": {"targets": ["main"]}},
        }
        error = subprocess.CalledProcessError(100, ["docker"], stderr=HISTORY)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error) as solve,
            patch.object(compose, "execute", return_value=json.dumps(INSPECTION)),
        ):
            self.assertEqual(
                self.call(root, plan)["main"]["containerimage.digest"], DIGEST
            )
            self.assertEqual(solve.call_count, 1)

    def test_receipt_write_failure_preserves_original_build_error(self):
        error = subprocess.CalledProcessError(100, ["docker"], stderr=HISTORY)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "execute_bake", side_effect=error),
            patch.object(compose, "execute", return_value=json.dumps(INSPECTION)),
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.call(root)
            self.assertIs(caught.exception, error)
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertFalse((Path(root) / "metadata.json").exists())

    def test_single_build_calls_allocate_distinct_native_publication_tags(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(compose, "start_builder"),
            patch.object(
                compose, "describe_image", side_effect=lambda _, reference: reference
            ),
        ):
            tags = []

            def solve(_args, plan, _path, _profile):
                tags.append(plan["target"]["single"]["tags"][0])
                return {"single": {"containerimage.digest": DIGEST}}

            with patch.object(compose, "bake_metadata", side_effect=solve):
                first = compose.image_build(root, PROFILE)
                second = compose.image_build(root, PROFILE)
            self.assertEqual(first, second)
            self.assertNotEqual(tags[0], tags[1])
            for tag in tags:
                self.assertRegex(tag, r":build-[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
