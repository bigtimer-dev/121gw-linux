import unittest

from gi.repository import GLib

from gw121.keys import MeterKey
from gw121.link import MeterLink


class FakeLink(MeterLink):
    def __init__(self, version: str) -> None:
        super().__init__(settings=None)
        self.is_connected = True
        self.firmware_version = version
        self.written: list[bytes] = []
        self.status: list[str] = []
        self.KEY_ECHO_MS = 30

    def write_frame(self, payload: bytes, done=None) -> None:
        self.written.append(payload)
        if done:
            done(None)

    def set_status(self, text: str) -> None:
        self.status.append(text)

    def press(self, key=MeterKey.HOLD):
        frame = key.payload()
        self.write_frame(frame)
        self.confirm_key(frame, key.title, lambda: self.write_frame(frame))
        return frame


def spin(ms: int) -> None:
    ctx = GLib.MainContext.default()
    end = GLib.get_monotonic_time() + ms * 1000
    while GLib.get_monotonic_time() < end:
        ctx.iteration(False)


class KeyEchoTests(unittest.TestCase):
    def test_echo_confirms(self):
        link = FakeLink("C-1.09")
        frame = link.press()
        link.ingest(frame[:2])
        link.ingest(frame[2:])                      # echo split over two notifications
        spin(120)
        self.assertEqual(link.written, [frame])
        self.assertIsNone(link._key_wait)

    def test_missing_echo_resent_once_on_c109(self):
        link = FakeLink("C-1.09")
        frame = link.press()
        spin(200)
        self.assertEqual(link.written, [frame, frame])   # once again, not more
        self.assertTrue(link.status[-1].endswith("did not confirm the key"))

    def test_resent_then_echoed(self):
        link = FakeLink("C-1.10")
        frame = link.press()
        spin(50)
        link.ingest(frame)
        spin(100)
        self.assertEqual(link.written, [frame, frame])
        self.assertIn("again", link.status[-1])

    def test_no_resend_on_older_firmware(self):
        for v in ("C-1.08", "F-1.17", "1.0"):
            link = FakeLink(v)
            frame = link.press()
            spin(100)
            self.assertEqual(link.written, [frame], v)   # a lost echo there could mean the key did arrive
            self.assertTrue(link.status[-1].endswith("did not confirm the key"), v)

    def test_not_on_page_ld(self):
        link = FakeLink("C-1.09")
        link.on_ld_page = True
        frame = link.press()
        spin(100)
        self.assertEqual(link.written, [frame])
        self.assertEqual(link.status, [])


if __name__ == "__main__":
    unittest.main()
