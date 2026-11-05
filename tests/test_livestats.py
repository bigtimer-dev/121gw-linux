import math
import statistics
import unittest

from gw121.livestats import LiveStats


class LiveStatsTests(unittest.TestCase):
    def test_matches_statistics_module(self):
        s, vals = LiveStats(), [1.0001, 1.0003, 0.9998, 1.0002, 1.0000, 0.9999]
        for v in vals:
            s.add("1:0", v, "V", now=10.0)
        self.assertEqual(s.n, 6)
        self.assertEqual((s.min, s.max), (0.9998, 1.0003))
        self.assertAlmostEqual(s.mean, statistics.mean(vals), places=12)
        self.assertAlmostEqual(s.std, statistics.stdev(vals), places=12)
        self.assertAlmostEqual(s.peak_to_peak, 0.0005, places=12)
        self.assertEqual(s.started, 10.0)

    def test_overload_is_skipped(self):
        s = LiveStats()
        s.add("1:0", 2.0, "V")
        s.add("1:0", math.nan, "V")
        self.assertEqual((s.n, s.skipped), (1, 1))
        self.assertIsNone(s.std)

    def test_restart_on_mode_or_unit(self):
        s = LiveStats()
        s.add("1:0", 2.0, "V")
        self.assertFalse(s.add("1:0", 3.0, "V"))
        self.assertTrue(s.add("2:0", 5.0, "mV"))
        self.assertEqual((s.n, s.min), (1, 5.0))

    def test_reset_keeps_the_mode(self):
        s = LiveStats()
        s.add("1:0", 2.0, "V")
        s.reset()
        self.assertIsNone(s.mean)
        self.assertFalse(s.add("1:0", 4.0, "V"))
        self.assertEqual(s.mean, 4.0)

    def test_long_run_is_stable(self):
        s = LiveStats()
        for i in range(200_000):
            s.add("1:0", 1000.0 + (0.001 if i % 2 else -0.001), "V")
        self.assertAlmostEqual(s.mean, 1000.0, places=9)
        self.assertAlmostEqual(s.std, 0.001, places=6)


if __name__ == "__main__":
    unittest.main()
