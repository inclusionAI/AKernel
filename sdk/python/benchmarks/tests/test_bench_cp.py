"""File benchmark must verify round trips and preserve cleanup failures."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from benchmarks import bench_cp


class _FakeFiles:
    def __init__(self):
        self.objects = {}
        self.removed = []
        self.corrupt_download = False

    def copy_from_local(self, source, target):
        self.objects[target] = Path(source).read_bytes()

    def write(self, target, data):
        self.objects[target] = data

    def read(self, path, format="text"):
        return self.objects[path]

    def copy_to_local(self, source, target):
        content = self.objects[source]
        if self.corrupt_download:
            content = b"corrupt"
        Path(target).write_bytes(content)

    def remove(self, path):
        self.removed.append(path)
        del self.objects[path]


class _RemoveFailureFiles(_FakeFiles):
    def remove(self, path):
        raise RuntimeError("remote cleanup failed")


class FileBenchmarkTest(unittest.TestCase):
    def test_both_upload_methods_verify_bytes_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_bytes(b"payload\x00\xff")
            files = _FakeFiles()
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            for method in ("copy_from_local", "write"):
                remote = f"/tmp/{method}"
                result = bench_cp.run_file_case(
                    files, source, remote, method=method, destination=Path(directory)
                )
                self.assertEqual(result["sha256"], expected)
                self.assertEqual(result["size_bytes"], source.stat().st_size)
                self.assertIn(remote, files.removed)
                self.assertNotIn(remote, files.objects)

    def test_corrupt_download_is_a_failure_and_still_cleans_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_bytes(b"payload")
            files = _FakeFiles()
            files.corrupt_download = True
            remote = "/tmp/roundtrip"
            with self.assertRaisesRegex(AssertionError, "hash mismatch"):
                bench_cp.run_file_case(
                    files,
                    source,
                    remote,
                    method="write",
                    destination=Path(directory),
                )
            self.assertIn(remote, files.removed)

    def test_cleanup_failure_cannot_be_reported_as_round_trip_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.write_bytes(b"payload")
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                bench_cp.run_file_case(
                    _RemoveFailureFiles(),
                    source,
                    "/tmp/roundtrip",
                    method="write",
                    destination=Path(directory),
                )


if __name__ == "__main__":
    unittest.main()
