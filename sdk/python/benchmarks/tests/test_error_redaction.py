"""Result files must never echo the caller's configured API key."""

import os
import unittest
from unittest.mock import patch

from benchmarks.harness.errors import safe_error


class ErrorRedactionTest(unittest.TestCase):
    def test_removes_api_key_from_error_sample(self):
        with patch.dict(os.environ, {"AKERNEL_TOKEN": "secret-example-key"}):
            message = safe_error(RuntimeError("request failed secret-example-key"))
        self.assertNotIn("secret-example-key", message)
        self.assertIn("<redacted>", message)


if __name__ == "__main__":
    unittest.main()
