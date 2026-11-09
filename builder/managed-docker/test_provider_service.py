"""A runtime capability cannot select another environment's collection."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from provider_service import RuntimeCollections


class CollectionAuthorization(unittest.TestCase):
    def managed_collection(self, root):
        for part in ["records", "leases", "owners", "retired"]:
            (root / part).mkdir()
        identity = "a" * 32
        token = identity + ".private-secret"
        record = root / "records" / (identity + ".json")
        record.write_text(
            json.dumps(
                {
                    "collection_id": identity,
                    "lifecycle_version": 1,
                    "registry": "registry.test",
                    "repository_prefix": "managed/" + identity + "/",
                    "scheme": "https",
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                }
            )
        )
        record.chmod(0o600)
        anchor = root / "leases" / identity
        anchor.touch(mode=0o600)
        os.link(anchor, root / "owners" / "source")
        return identity, token, anchor

    def test_metadata_only_operation_uses_same_capability_and_holds_retirement_lock(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity, token, anchor = self.managed_collection(root)
            broker = RuntimeCollections(root, "/private/image.sock")
            artifact = {
                "reference": "registry.test/managed/"
                + identity
                + "/single@sha256:"
                + "a" * 64
            }

            def describe(reference):
                with anchor.open() as candidate:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return {"reference": reference}

            with patch("provider_service.ImageProvider") as factory:
                factory.return_value.describe_artifact.side_effect = describe
                self.assertEqual(
                    broker.perform(
                        "Bearer " + token, "/describe", artifact["reference"]
                    ),
                    artifact,
                )
                self.assertEqual(factory.call_args.kwargs["owner"], identity)
                factory.return_value.prepare.assert_not_called()

    def test_deleted_owner_and_retired_capability_cannot_mount(self):
        for state in ["unowned", "retired", "missing"]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                identity, token, anchor = self.managed_collection(root)
                if state == "unowned":
                    (root / "owners" / "source").unlink()
                elif state == "retired":
                    (root / "retired" / identity).touch()
                else:
                    anchor.unlink()
                with patch("provider_service.ImageProvider") as factory:
                    with self.assertRaises(PermissionError):
                        RuntimeCollections(root, "/private/image.sock").perform(
                            "Bearer " + token, "/prepare", "immutable"
                        )
                    factory.assert_not_called()

    def test_finished_operations_do_not_keep_collection_provider_cache_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _identity, token, _anchor = self.managed_collection(root)
            broker = RuntimeCollections(root, "/private/image.sock")
            with patch(
                "provider_service.ImageProvider.describe_artifact",
                return_value={"reference": "verified"},
            ):
                self.assertEqual(
                    broker.perform("Bearer " + token, "/describe", "verified"),
                    {"reference": "verified"},
                )
            self.assertEqual(len(broker.providers), 0)

    def test_invalid_or_foreign_capability_cannot_prepare_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "records").mkdir()
            identity = "a" * 32
            token = identity + ".private-secret"
            record = root / "records" / (identity + ".json")
            record.write_text(
                json.dumps(
                    {
                        "collection_id": identity,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    }
                )
            )
            record.chmod(0o600)
            broker = RuntimeCollections(root, "/private/image.sock")
            for auth in [
                "",
                "Basic secret",
                "Bearer ../node.secret",
                "Bearer " + identity + ".wrong",
                "Bearer " + "b" * 32 + ".private-secret",
            ]:
                with self.subTest(auth=auth), self.assertRaises(PermissionError):
                    broker.authorize(auth)
            self.assertEqual(broker.providers, {})
            record.chmod(0o644)
            with self.assertRaises(PermissionError):
                broker.authorize("Bearer " + token)
