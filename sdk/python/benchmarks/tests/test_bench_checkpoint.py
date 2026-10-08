"""RRT checkpoint completion is required before measuring reload."""

import unittest

from benchmarks.bench_checkpoint import verify_checkpoint_response


class CheckpointResponseTest(unittest.TestCase):
    def test_completed_response_is_accepted(self):
        verify_checkpoint_response('{"status":"completed"}')

    def test_other_status_does_not_authorize_reload(self):
        for response in ('{"status":"failed"}', '{"status":"pending"}', "{}"):
            with self.subTest(response=response):
                with self.assertRaisesRegex(AssertionError, "did not complete"):
                    verify_checkpoint_response(response)


if __name__ == "__main__":
    unittest.main()
