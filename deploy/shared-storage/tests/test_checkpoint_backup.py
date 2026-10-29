#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Backup corruption, path confinement and failed-stream completion boundaries."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

SPEC = importlib.util.spec_from_file_location("checkpoint_backup", Path(__file__).resolve().parents[1] / "backup/backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class Bucket:
    def __init__(self):
        self.complete = False
        self.aborted = False

    def init_multipart_upload(self, key):
        return SimpleNamespace(upload_id="owned-upload")

    def upload_part(self, *args):
        return SimpleNamespace(etag="part-hash")

    def complete_multipart_upload(self, *args, **kwargs):
        assert kwargs["headers"]["x-oss-forbid-overwrite"] == "true"
        self.complete = True

    def abort_multipart_upload(self, *args):
        self.aborted = True


class BackupBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "cached-layout").write_text("cached-filesystem-v1\n")
        self.identity = str(uuid.uuid4())
        self.base = self.root / "artifacts" / self.identity
        self.files = self.base / "files"
        self.files.mkdir(parents=True)
        content = b"recoverable-business-state"
        (self.files / "state").write_bytes(content)
        self.manifest = {"version": 1, "size": len(content), "directories": [], "files": [{
            "path": "state", "size": len(content), "mode": 0o600,
            "sha256": hashlib.sha256(content).hexdigest(),
        }]}
        self.expected = {self.identity: {"size_bytes": len(content)}}
        self.save_manifest()

    def save_manifest(self):
        (self.base / "manifest.json").write_text(json.dumps(self.manifest))

    def test_export_contains_only_committed_referenced_files(self):
        (self.root / "artifacts" / "unfinished-upload").mkdir()
        paths = backup.validate_fileset(self.root, self.expected)
        self.assertIn(f"artifacts/{self.identity}/files/state", paths)
        self.assertFalse(any("unfinished-upload" in p for p in paths))

    def test_damaged_or_missing_referenced_artifact_fails_closed(self):
        (self.files / "state").write_bytes(b"damaged")
        with self.assertRaises(RuntimeError):
            backup.validate_fileset(self.root, self.expected)
        (self.base / "manifest.json").unlink()
        with self.assertRaises(FileNotFoundError):
            backup.validate_fileset(self.root, self.expected)

    def test_manifest_cannot_escape_or_follow_symlinks(self):
        for unsafe in ("../outside", "/outside", "a//b", "a/./b", "a\\b"):
            self.manifest["files"][0]["path"] = unsafe
            self.save_manifest()
            with self.assertRaises(ValueError):
                backup.validate_fileset(self.root, self.expected)
        self.manifest["files"][0]["path"] = "state"
        self.save_manifest()
        (self.files / "state").unlink()
        (self.files / "state").symlink_to(self.root / "cached-layout")
        with self.assertRaises(RuntimeError):
            backup.validate_fileset(self.root, self.expected)

    def test_directory_entries_cannot_smuggle_symlinks(self):
        (self.files / "external").symlink_to(self.root, target_is_directory=True)
        self.manifest["directories"] = ["external"]
        self.save_manifest()
        with self.assertRaises(RuntimeError):
            backup.validate_fileset(self.root, self.expected)

    def test_duplicate_paths_and_invalid_modes_are_rejected(self):
        self.manifest["files"].append(dict(self.manifest["files"][0]))
        self.save_manifest()
        with self.assertRaises(RuntimeError):
            backup.validate_fileset(self.root, self.expected)
        self.manifest["files"].pop()
        self.manifest["files"][0]["mode"] = 0o4777
        self.save_manifest()
        with self.assertRaises(RuntimeError):
            backup.validate_fileset(self.root, self.expected)

    def test_export_command_failure_aborts_without_completion(self):
        bucket = Bucket()
        def failed_export():
            raise RuntimeError("exporter exited unsuccessfully")
        with self.assertRaises(RuntimeError):
            backup.upload(bucket, "owned/payload", io.BytesIO(b"partial archive"), before_complete=failed_export)
        self.assertTrue(bucket.aborted)
        self.assertFalse(bucket.complete)

    def test_mount_loss_during_stream_aborts_without_completion(self):
        bucket = Bucket()
        def missing_mount():
            raise RuntimeError("mount lost")
        with self.assertRaises(RuntimeError):
            backup.upload(bucket, "owned/payload", io.BytesIO(b"state"), check=missing_mount)
        self.assertTrue(bucket.aborted)
        self.assertFalse(bucket.complete)

    def test_successful_stream_has_complete_hash_and_no_abort(self):
        bucket = Bucket()
        data = b"complete state"
        result = backup.upload(bucket, "owned/payload", io.BytesIO(data))
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(result["size"], len(data))
        self.assertTrue(bucket.complete)
        self.assertFalse(bucket.aborted)


if __name__ == "__main__":
    unittest.main()
