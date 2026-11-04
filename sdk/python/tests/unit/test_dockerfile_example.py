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

"""Dockerfile E2E example configuration contract."""

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


def _example_module():
    path = Path(__file__).resolve().parents[2] / "examples" / "dockerfile_launch.py"
    spec = importlib.util.spec_from_file_location("dockerfile_launch_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerfileExampleTest(unittest.TestCase):
    def test_base_image_can_use_cluster_registry(self):
        module = _example_module()
        image = "swr.example.test/akernel/e2e@sha256:0123456789abcdef"
        with patch.dict(os.environ, {"AKERNEL_TEST_DOCKERFILE_IMAGE": image}):
            rendered = module._dockerfile("FROM ubuntu:22.04\nRUN true\n")

        self.assertEqual(rendered, f"FROM {image}\nRUN true\n")


if __name__ == "__main__":
    unittest.main()
