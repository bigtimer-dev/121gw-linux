import unittest
from datetime import datetime

from gw121.keys import clock_payload
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


if __name__ == "__main__":
    unittest.main()
