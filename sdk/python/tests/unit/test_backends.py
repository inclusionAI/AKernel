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

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from akernel_sdk._backends import registry
from akernel_sdk._backends.errors import (
    BackendNotInstalledError,
    InvalidBackendError,
)


class RegistryTest(unittest.TestCase):
    def test_explicit_selection_wins_without_importing_backend(self):
        with (
            patch.dict(
                os.environ,
                {"AKERNEL_BACKEND": "openyuanrong-sandbox"},
                clear=True,
            ),
            patch.object(registry, "_is_installed") as installed,
        ):
            self.assertEqual(registry._select_backend(), "adx")
        installed.assert_not_called()

    def test_previous_actor_selector_uses_adx(self):
        with patch.dict(
            os.environ, {"AKERNEL_BACKEND": "openyuanrong-sdk"}, clear=True
        ):
            self.assertEqual(registry._select_backend(), "adx")

    def test_sandbox_has_auto_detection_priority(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(registry, "_is_installed", return_value=True) as installed,
        ):
            self.assertEqual(registry._select_backend(), "adx")
        installed.assert_called_once_with("adx-sandbox")

    def test_invalid_explicit_backend_fails_during_selection(self):
        with (
            patch.dict(os.environ, {"AKERNEL_BACKEND": "sandbox"}, clear=True),
            self.assertRaisesRegex(InvalidBackendError, "adx"),
        ):
            registry._select_backend()

    def test_missing_default_backend_recommends_plain_install(self):
        error = registry._not_installed_error("adx")
        self.assertIsInstance(error, BackendNotInstalledError)
        self.assertIn("pip install akernel-sdk", str(error))
        self.assertNotIn("[openyuanrong-sandbox]", str(error))

    def test_loaded_backend_close_is_registered_for_process_exit(self):
        backend = MagicMock()
        backend_module = SimpleNamespace(
            create_backend=MagicMock(return_value=backend),
        )
        with (
            patch.object(registry, "_loaded_backend", None),
            patch.object(
                registry,
                "_selected_backend",
                "adx",
            ),
            patch.object(registry, "_is_installed", return_value=True),
            patch.object(
                registry.importlib,
                "import_module",
                return_value=backend_module,
            ),
            patch.object(registry, "_config_from_env", return_value=MagicMock()),
            patch.object(registry.atexit, "register") as register,
        ):
            self.assertIs(registry.load_backend(), backend)

        register.assert_called_once_with(backend.close)


if __name__ == "__main__":
    unittest.main()
