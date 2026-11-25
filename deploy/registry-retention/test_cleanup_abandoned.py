"""Safety checks for cleaning fenced Distribution repository metadata."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cleanup_abandoned import LAYOUT, cleanup


class AbandonedFilesystemTests(TestCase):
    def setUp(self):
        data = TemporaryDirectory()
        self.addCleanup(data.cleanup)
        self.root = Path(data.name)
        self.repositories = self.root.joinpath(*LAYOUT)

    def write(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"registry data")
        return path

    @staticmethod
    def name(character: str) -> str:
        return "akernel-ephemeral/" + character * 32 + "/service-0"

    def test_cleanup_is_scoped_idempotent_and_protects_current_tags(self):
        orphan, tagged, missing, foreign, nested = map(self.name, "abcde")
        upload = self.write(self.repositories / orphan / "_uploads/upload/data")
        self.write(self.repositories / orphan / "_manifests/tags/build/index/link")
        tag = self.write(self.repositories / tagged / "_manifests/tags/kept/current/link")
        unlisted = self.write(self.repositories / foreign / "_uploads/upload/data")
        child = self.write(self.repositories / nested / "another/_uploads/upload/data")
        shared = self.write(self.repositories.parent / "blobs/sha256/aa/digest/data")
        plan = {"abandoned_repositories": [orphan, tagged, missing, nested]}
        expected = {"cleaned_repositories": [orphan, missing]}
        self.assertEqual(cleanup(self.root, plan), expected)
        self.assertEqual(cleanup(self.root, plan), expected)
        self.assertFalse(upload.exists())
        self.assertFalse((self.repositories / orphan).parent.exists())
        for path in (tag, unlisted, child, shared):
            self.assertTrue(path.is_file())

    def test_invalid_batch_is_rejected_before_changes(self):
        name = self.name("a")
        upload = self.write(self.repositories / name / "_uploads/upload/data")
        for invalid in ("../other", name):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cleanup(self.root, {"abandoned_repositories": [name, invalid]})
            self.assertTrue(upload.is_file())

    def test_symlinked_repository_or_metadata_is_deferred(self):
        outside = self.write(self.root / "outside" / "_uploads/upload/data")
        for candidate, stash in [(self.name("a"), ""), (self.name("b"), "_uploads")]:
            path = self.repositories / candidate / stash
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(outside.parent.parent if not stash else outside.parent)
            with patch("cleanup_abandoned.sys.stderr"):
                self.assertEqual(
                    cleanup(self.root, {"abandoned_repositories": [candidate]}),
                    {"cleaned_repositories": []},
                )
            self.assertTrue(path.is_symlink())
            self.assertTrue(outside.is_file())
