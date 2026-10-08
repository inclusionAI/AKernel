"""Port benchmark must read the intended resident sandbox's response."""

import unittest
from unittest.mock import patch

from benchmarks import bench_http


class _Response:
    def __init__(self, body):
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return self.body


class HttpBenchmarkTest(unittest.TestCase):
    def test_verified_response_is_counted(self):
        with patch.object(
            bench_http.urllib.request, "urlopen", return_value=_Response(b"abc")
        ):
            measurement = bench_http.run_http_case("http://127.0.0.1:9999", b"abc")
        self.assertEqual(measurement["bytes"], 3)
        self.assertGreaterEqual(measurement["elapsed_seconds"], 0)

    def test_wrong_response_is_failure(self):
        with patch.object(
            bench_http.urllib.request, "urlopen", return_value=_Response(b"other")
        ):
            with self.assertRaisesRegex(AssertionError, "body mismatch"):
                bench_http.run_http_case("http://127.0.0.1:9999", b"abc")


if __name__ == "__main__":
    unittest.main()
