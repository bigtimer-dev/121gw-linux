import math
import random
import unittest

import cairo

from gw121.chart import FAN, Palette, Summary, _columns, draw_chart
from gw121.packet import PacketV2, build_frame
from gw121.sessions import DataPoint


def make_points(n, seed=1):
    rnd = random.Random(seed)
    pts = []
    for i in range(n):
        v = rnd.randint(0, 49_999)
        ovf = rnd.random() < 0.02
        p = PacketV2.try_parse(build_frame(1, v, 1, overflow=ovf, sub_mode=5, sub_value=200 + i % 90, sub_point=1))
        pts.append(DataPoint.from_packet(p, ts=i * 0.2))
    return pts


class SummaryTests(unittest.TestCase):
    def test_incremental_equals_rebuild(self):
        pts = make_points(3000)
        grow, s = [], Summary()
        for p in pts:
            grow.append(p)
            s.sync(grow)
        full = Summary()
        full.sync(list(pts))
        for k in range(len(full.levels)):
            for arr_a, arr_b in zip(s.levels[k], full.levels[k]):
                self.assertEqual(len(arr_a), len(arr_b))
                for x, y in zip(arr_a, arr_b):
                    self.assertTrue(x == y or (isinstance(x, float) and math.isclose(x, y, rel_tol=1e-9)))

    def test_columns_match_brute_force(self):
        pts = make_points(25_000, seed=7)
        s = Summary()
        s.sync(pts)
        for a, b, cols in ((0, 24_999, 1000), (1234, 20_000, 640), (5000, 5600, 100), (100, 1000, 900)):
            data = _columns(s, a, b, cols)
            vals = [p.plot_value for p in pts[a : b + 1] if math.isfinite(p.plot_value)]
            # global extremes of the range are preserved (buckets may include a few edge samples)
            self.assertLessEqual(min(data.lo), min(vals))
            self.assertGreaterEqual(max(data.hi), max(vals))
            if (b - a + 1) / cols > FAN:
                self.assertLessEqual(len(data.xs), cols)

    def test_rebuild_when_list_replaced_or_trimmed(self):
        pts = make_points(100)
        s = Summary()
        s.sync(pts)
        trimmed = pts[50:]
        s.sync(trimmed)
        self.assertEqual(len(s), 50)
        self.assertEqual(s.vals[0], trimmed[0].plot_value if math.isfinite(trimmed[0].plot_value) else s.vals[0])

    def test_draw_with_window_and_zoom(self):
        pts = make_points(30_000)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 240)
        cr = cairo.Context(surf)
        pal = Palette((0, 0, 0), (0, 0, 0, .6), (0, 0, 0, .1), (.2, .5, .9), bg=(1, 1, 1))
        for span, start, auto in ((1, 0, False), (0.05, 0.95, True), (0.02, 0.3, False)):
            draw_chart(cr, 800, 240, pts, "V", show_temperature=True, span=span, start=start, pal=pal,
                       autoscale=auto, window=25_000, hover_x=400)
        draw_chart(cr, 800, 240, [], "V", show_temperature=True, span=1, start=0, pal=pal)


if __name__ == "__main__":
    unittest.main()
