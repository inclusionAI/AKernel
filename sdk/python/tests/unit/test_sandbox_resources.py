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

import unittest

from akernel_sdk._sandbox_resources import (
    MAX_STORAGE_MB,
    normalize_xpu,
    storage_bytes,
    validate_storage_mb,
    xpu_custom_resource,
)


class SandboxResourcesTest(unittest.TestCase):
    def test_xpu_is_canonical_and_exact_model_is_escaped(self):
        self.assertEqual(normalize_xpu(" GPU:A10.2:02 "), "gpu:a10.2:2")
        self.assertEqual(
            xpu_custom_resource("gpu:a10.2:2"),
            (r"GPU/a10\.2/count", 2.0),
        )

    def test_storage_wire_value_and_upper_bound(self):
        self.assertEqual(storage_bytes(256), float(256 * 1024 * 1024))
        validate_storage_mb(MAX_STORAGE_MB)
        with self.assertRaises(ValueError):
            validate_storage_mb(MAX_STORAGE_MB + 1)


if __name__ == "__main__":
    unittest.main()
