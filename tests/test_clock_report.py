import unittest

from gw121.keys import CLOCK_REQUEST, parse_clock_report
from gw121.packet import FrameAssembler, build_frame


def fb(boot=4202440, hz=4202440, now=4201766, trim=0, status=1, steps=4, brr=219) -> bytes:
    f = bytes([0xFB]) + boot.to_bytes(4, "little") + hz.to_bytes(4, "little") + bytes([trim, status]) + \
        steps.to_bytes(2, "little") + brr.to_bytes(2, "little") + now.to_bytes(4, "little")
    x = 0
    for b in f:
        x ^= b
    return f + bytes([x])


class ClockReportTests(unittest.TestCase):
    def test_request_is_key_code_96(self):
        self.assertEqual(CLOCK_REQUEST, b"\xf4" + b"9696")

    def test_parse_after_echo_and_readings(self):
        buf = CLOCK_REQUEST + build_frame(1, 123) + fb()
        r = parse_clock_report(buf)
        self.assertEqual((r.hz, r.now_hz, r.brr, r.status), (4202440, 4201766, 219, 1))
        self.assertAlmostEqual(r.baud, 19189.2, places=0)
        self.assertIn("+1779 ppm", r.text())

    def test_damaged_frame_ignored(self):
        bad = bytearray(fb())
        bad[6] ^= 0x10
        self.assertIsNone(parse_clock_report(bytes(bad)))

    def test_not_measured_now_uses_set_value(self):
        self.assertIn("4.2024 MHz", parse_clock_report(fb(now=0)).text())


class RejectCounterTests(unittest.TestCase):
    def test_damaged_reading_counted(self):
        a = FrameAssembler()
        good = build_frame(1, 5)
        bad = bytearray(build_frame(1, 6))
        bad[5] ^= 0x01
        a.feed(good + bytes(bad) + good)
        self.assertEqual(a.rejected, 1)

    def test_clean_stream_has_none(self):
        a = FrameAssembler()
        a.feed(b"".join(build_frame(1, v) for v in range(50)))
        self.assertEqual(a.rejected, 0)


if __name__ == "__main__":
    unittest.main()
