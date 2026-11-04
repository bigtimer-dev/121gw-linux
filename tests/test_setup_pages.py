import time
import unittest

from gw121.link import MeterLink
from gw121.packet import build_frame
from gw121.setup_pages import PAGES, parse_setup_frames


def frame(item, text, flags=0, units=0) -> bytes:
    b = bytes([0xF7, item]) + text.encode("latin1") + bytes([flags, units, 0])
    x = 0
    for c in b:
        x ^= c
    return b + bytes([x])


class ParseTests(unittest.TestCase):
    def test_values(self):
        cases = [
            (frame(21, "bt 20"), "Bluetooth rate", "20"),
            (frame(7, "13 19"), "Time", "13:19"),             # the separator blinks
            (frame(7, " 9-05"), "Time", "9:05"),
            (frame(1, "BAT60", 1, 1), "Battery", "6.0 V"),
            (frame(37, "L4-20"), "4–20 mA loop view", "4–20 mA as %"),
            (frame(28, "90000", 4, 1), "Low limit", "9.0000 V"),
            (frame(2, "APOoN", 2), "Auto power-off", "on"),
            (frame(3, "b-OFF"), "Beeper", "off"),
            (frame(4, "Lcd-4"), "LCD contrast", "4"),
            (frame(27, "LtOFF"), "Limit test", "OFF"),
            (frame(38, "P1000"), "PT100 / PT1000 view", "PT1000"),
            (frame(20, "bL 15"), "Backlight auto-off", "15"),
            (frame(28, "Lo---"), "Low limit", "not captured"),
        ]
        for f, name, value in cases:
            st = parse_setup_frames(f)[0][0]
            self.assertEqual((st.name, st.value), (name, value), f)

    def test_flags_and_description(self):
        st = parse_setup_frames(frame(28, "12345", 2 | 0x10 | 0x20, 0x04 | 0x02))[0][0]
        self.assertTrue(st.negative and st.editing)
        self.assertEqual(st.shown, "-123.45")
        self.assertEqual(st.unit, "mA")
        self.assertIn("capture", st.description)

    def test_stream_with_status_frames_split_anywhere(self):
        data = build_frame(1, 12345, 1) + frame(24, "dSP 2") + build_frame(1, 12346, 1) + frame(0, "  250")
        got = []
        buf = b""
        for i in range(0, len(data), 7):
            buf += data[i:i + 7]
            frames, done = parse_setup_frames(buf)
            got += frames
            buf = buf[done:]
        self.assertEqual([(f.item, f.chars) for f in got], [(24, "dSP 2"), (0, "  250")])

    def test_bad_checksum_ignored(self):
        f = bytearray(frame(21, "bt 20"))
        f[3] ^= 1
        self.assertEqual(parse_setup_frames(bytes(f))[0], [])

    def test_every_page_has_a_text(self):
        for item, (_short, title, text) in PAGES.items():
            self.assertTrue(title and text, item)


class FakeLink(MeterLink):
    def __init__(self) -> None:
        super().__init__(settings=None)
        self.is_connected = True

    def write_frame(self, payload: bytes, done=None) -> None:
        if done:
            done(None)


class LinkTests(unittest.TestCase):
    def test_page_shown_then_left(self):
        link = FakeLink()
        link.ingest(build_frame(1, 100, 1) + frame(21, "bt 20")[:6])
        link.ingest(frame(21, "bt 20")[6:])
        self.assertEqual(link.setup_text.value, "20")
        link.ingest(frame(0, "  250") + build_frame(1, 100, 1))
        self.assertIsNone(link.setup_text)

    def test_expires_when_the_frames_stop(self):
        link = FakeLink()
        link.ingest(frame(24, "dSP 1"))
        self.assertIsNotNone(link.setup_text)
        link._setup_at = time.monotonic() - 10
        link.ingest(build_frame(1, 100, 1))
        self.assertIsNone(link.setup_text)


if __name__ == "__main__":
    unittest.main()
