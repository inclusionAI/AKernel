"""Check bounded aggregation across a long stream and multiple workers."""

import unittest

from benchmarks.harness.stats import LatencyHistogram


class LatencyHistogramTest(unittest.TestCase):
    def test_large_stream_keeps_fixed_storage_and_correct_count(self):
        histogram = LatencyHistogram()
        for index in range(100_000):
            histogram.observe(0.001 if index < 90_000 else 0.5)

        self.assertEqual(histogram.count, 100_000)
        self.assertLessEqual(len(histogram.counts), 40)
        self.assertEqual(sum(histogram.counts), histogram.count)
        self.assertEqual(histogram.percentile_upper_ms(0.9), 1.0)
        self.assertEqual(histogram.percentile_upper_ms(0.99), 500.0)

    def test_merge_preserves_all_samples_and_tail(self):
        first = LatencyHistogram()
        second = LatencyHistogram()
        first.observe(0.002)
        second.observe(2.0)
        first.merge(second)

        self.assertEqual(first.count, 2)
        self.assertEqual(first.summary()["max_ms"], 2000.0)
        self.assertEqual(first.percentile_upper_ms(1), 2000.0)

    def test_ten_second_sessions_have_useful_tail_bucket(self):
        histogram = LatencyHistogram()
        histogram.observe(10.45)
        self.assertEqual(histogram.percentile_upper_ms(0.99), 12000.0)


if __name__ == "__main__":
    unittest.main()
