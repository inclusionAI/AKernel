# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_adx_dependency import prepare, read_lock


class PrepareAdxDependencyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.version = "0.1.0"
        self.source = self.root / f"adx_sandbox-{self.version}-py3-none-any.whl"
        self.source.write_bytes(b"verified wheel bytes")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_lock(self, *, digest: str | None = None) -> Path:
        lock = self.root / "adx-sdk.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "adx-sandbox",
                    "version": self.version,
                    "commit": "a" * 40,
                    "url": self.source.as_uri(),
                    "sha256": digest
                    or hashlib.sha256(self.source.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        return lock

    def test_verified_wheel_is_downloaded_and_reused(self):
        lock = self._write_lock()
        output = self.root / "download"

        first = prepare(lock, output)
        second = prepare(lock, output)

        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), self.source.read_bytes())
        self.assertEqual(read_lock(lock).version, self.version)

    def test_checksum_mismatch_does_not_publish_wheel(self):
        lock = self._write_lock(digest="0" * 64)
        output = self.root / "download"

        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            prepare(lock, output)

        self.assertEqual(list(output.glob("*.whl")), [])
        self.assertEqual(list(output.glob("*.tmp")), [])

    def _archive_lock(self, *, symlink=False):
        lock = self._write_lock()
        archive = self.root / "adx-release.tar.gz"
        member = f"sdk/{self.source.name}"
        with tarfile.open(archive, "w:gz") as bundle:
            info = tarfile.TarInfo(f"./{member}")
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                bundle.addfile(info)
            else:
                payload = self.source.read_bytes()
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
        data = json.loads(lock.read_text())
        data.update(
            url=archive.as_uri(),
            archive_member=member,
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        lock.write_text(json.dumps(data))
        return lock, archive

    def test_release_bundle_wheel_is_verified_and_cached(self):
        lock, archive = self._archive_lock()
        output = self.root / "download"
        wheel = prepare(lock, output)
        self.assertEqual(wheel.name, self.source.name)
        self.assertEqual(wheel.read_bytes(), self.source.read_bytes())
        archive.unlink()
        self.assertEqual(prepare(lock, output), wheel)
        self.assertEqual(list(output.glob("*.tmp")), [])

    def test_release_bundle_and_wheel_checksums_are_both_required(self):
        for field in ("archive_sha256", "sha256"):
            with self.subTest(field=field):
                lock, _ = self._archive_lock()
                data = json.loads(lock.read_text())
                data[field] = "0" * 64
                lock.write_text(json.dumps(data))
                output = self.root / "download"
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    prepare(lock, output)
                self.assertEqual(list(output.iterdir()), [])

    def test_release_bundle_rejects_non_regular_wheel(self):
        lock, _ = self._archive_lock(symlink=True)
        output = self.root / "download"
        with self.assertRaisesRegex(ValueError, "regular file"):
            prepare(lock, output)
        self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
