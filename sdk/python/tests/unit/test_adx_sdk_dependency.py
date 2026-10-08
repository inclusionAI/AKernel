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
import json
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


if __name__ == "__main__":
    unittest.main()
