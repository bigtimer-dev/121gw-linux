import unittest
from datetime import datetime

from gw121.keys import VERSION_REQUEST, clock_applies_every_time, clock_payload, parse_version_answer
from gw121.link import MeterLink
from gw121.packet import build_frame


class FakeLink(MeterLink):
    def __init__(self) -> None:
        super().__init__(settings=None)
        self.is_connected = True
        self.written: list[bytes] = []

    def write_frame(self, payload: bytes, done=None) -> None:
        self.written.append(payload)
        if done:
            done(None)


class ClockPayloadTests(unittest.TestCase):
    def test_frame_layout(self):
        # the frame the emulated meter (zombu4 firmware C-0.94) applied: SETUP pages showed 2031, 07-09, 13-45
        self.assertEqual(clock_payload(datetime(2031, 7, 9, 13, 45, 20)), b"\xf81F07090D2D1425")

    def test_checksum_and_hex(self):
        f = clock_payload(datetime(2026, 11, 3, 21, 5, 30))
        self.assertEqual(len(f), 15)
        v = [int(f[i:i + 2], 16) for i in range(1, 15, 2)]
        self.assertEqual(v[:6], [26, 11, 3, 21, 5, 30])
        self.assertEqual(v[6], 26 ^ 11 ^ 3 ^ 21 ^ 5 ^ 30)
        self.assertTrue(f[1:].isupper() or f[1:].isdigit())

    def test_year_outside_meter_range(self):
        with self.assertRaises(ValueError):
            clock_payload(datetime(2100, 1, 1))


class ClockSyncTests(unittest.TestCase):
    def setUp(self):
        self.link = FakeLink()
        self.results: list[str] = []

    def tearDown(self):
        self.link._clock_finish(None)

    def test_echo_split_and_mixed_with_status_frames(self):
        self.link.sync_clock(self.results.append)
        frame = self.link.written[-1]
        stream = build_frame(1, 100, 1) + frame + build_frame(1, 101, 1)
        self.link.ingest(stream[:25])
        self.assertEqual(self.results, [])
        self.link.ingest(stream[25:])
        self.assertEqual(len(self.results), 1)
        self.assertTrue(self.results[0].startswith("Meter clock set to "), self.results[0])
        self.assertEqual(self.link.latest.main_int_value, 101)  # status frames still decoded around the echo

    def test_no_echo(self):
        self.link.sync_clock(self.results.append)
        self.link.ingest(build_frame(1, 100, 1))
        self.link._clock_timed_out()
        self.assertEqual(self.results, ["The meter did not confirm the clock update"])

    def test_second_sync_on_same_connection_explains_the_meter_rule(self):
        self.link.sync_clock(self.results.append)
        self.link.ingest(self.link.written[-1])
        self.link.sync_clock(self.results.append)
        self.link.ingest(self.link.written[-1])
        self.assertIn("each time its Bluetooth is switched on", self.results[-1])

    def test_disconnect_cancels(self):
        self.link.sync_clock(self.results.append)
        self.link._reset_link_state()
        self.assertEqual(self.results, ["Clock update cancelled: disconnected"])
        self.link.is_connected = True
        self.link.sync_clock(self.results.append)
        self.link.ingest(self.link.written[-1])
        self.assertTrue(self.results[-1].startswith("Meter clock set to "))  # new connection: first sync again

    def test_not_connected(self):
        self.link.is_connected = False
        self.link.sync_clock(self.results.append)
        self.assertEqual((self.link.written, self.results), ([], ["Not connected"]))


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.link = FakeLink()
        self.results: list[str] = []

    def tearDown(self):
        self.link._clock_finish(None)

    @staticmethod
    def answer(text: bytes) -> bytes:
        a = bytes([0xF6]) + text
        x = 0
        for b in a:
            x ^= b
        return a + bytes([x])

    def test_parse(self):
        self.assertEqual(parse_version_answer(b"\x00" + self.answer(b"C-102") + b"\xf2"), "C-1.02")
        self.assertIsNone(parse_version_answer(self.answer(b"C-102")[:-1] + b"\x00"))      # bad XOR
        self.assertTrue(clock_applies_every_time("F-1.01"))
        self.assertTrue(clock_applies_every_time("C-1.02"))
        self.assertFalse(clock_applies_every_time("C-0.96"))
        self.assertFalse(clock_applies_every_time(None))

    def test_asked_on_the_link_and_answer_split_between_status_frames(self):
        self.link.ingest(build_frame(1, 100, 1))
        self.assertEqual(self.link.written, [])
        self.link.ingest(build_frame(1, 100, 1))
        self.assertEqual(self.link.written, [VERSION_REQUEST])
        stream = VERSION_REQUEST + self.answer(b"C-102") + build_frame(1, 101, 1)
        self.link.ingest(stream[:8])
        self.link.ingest(stream[8:])
        self.assertEqual(self.link.firmware_version, "C-1.02")
        self.assertEqual(self.link.latest.main_int_value, 101)

    def test_old_firmware_asked_twice_then_left_alone(self):
        for _ in range(30):
            self.link.ingest(build_frame(1, 100, 1))
        self.assertEqual(self.link.written, [VERSION_REQUEST, VERSION_REQUEST])
        self.assertIsNone(self.link.firmware_version)

    def test_second_sync_on_v101_is_reported_as_set(self):
        self.link.firmware_version = "C-1.02"
        self.link.sync_clock(self.results.append)
        self.link.ingest(self.link.written[-1])
        self.link.sync_clock(self.results.append)
        self.link.ingest(self.link.written[-1])
        self.assertTrue(self.results[-1].startswith("Meter clock set to "), self.results[-1])


if __name__ == "__main__":
    unittest.main()
