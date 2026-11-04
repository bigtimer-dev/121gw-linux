import struct
import unittest

from gw121 import upload as U
from gw121.demo import DemoSdReceiver
from gw121.link import MeterLink


def image(size=20000, sp=0x20014000, reset=0x08006101) -> bytes:
    body = bytes((i * 7 + 3) & 0xFF for i in range(size - 8))
    return struct.pack("<II", sp, reset) + body


def run(session, meter, mangle=None, limit=10_000):
    """Drive a session against a receiver; mangle(n, packet) may return a changed packet or None (lost)."""
    parser = U.AnswerParser()
    for n in range(limit):
        if session.done:
            return n
        p = session.step().packet
        if mangle:
            p = mangle(n, p)
        answer = meter.packet(p) if p is not None else None
        got = parser.feed(answer) if answer else []
        if not got:
            if not session.timeout():
                return n
            continue
        session.answer(*got[0])
    raise AssertionError("did not finish")


class ProtocolTests(unittest.TestCase):
    def test_crc16_is_ccitt_false(self):
        self.assertEqual(U.crc16(b"123456789"), 0x29B1)

    def test_packet_layout(self):
        p = U.packet(U.T_DATA, 0x0102, b"abc")
        self.assertEqual(p[:7], bytes([0xB5, 0x5B, 2, 0x02, 0x01, 3]) + b"a")
        self.assertEqual(len(p), 8 + 3)
        with self.assertRaises(ValueError):
            U.packet(U.T_DATA, 0, bytes(233))

    def test_check_image(self):
        self.assertIsNone(U.check_image(image()))
        self.assertIn("size", U.check_image(image(size=0x100)))
        self.assertIn("stack pointer", U.check_image(image(sp=0)))
        self.assertIn("reset vector", U.check_image(image(reset=0x08006100)))      # not Thumb
        self.assertIn("reset vector", U.check_image(image(reset=0x08000101)))      # bootloader area

    def test_image_version(self):
        x = 0
        for b in b"\xF6C-102":
            x ^= b
        self.assertEqual(U.image_version(image() + b"\xF6C-102" + bytes([x])), "C-1.02")
        self.assertIsNone(U.image_version(image()))

    def test_answer_parser_skips_noise_and_split_answers(self):
        body = struct.pack("<BHBBH", 0x81, 7, 3, 0, 8)
        a = b"\xB5\x5B" + body + struct.pack("<H", U.crc16(body))
        parser = U.AnswerParser()
        self.assertEqual(parser.feed(b"\xF2\x00\xB5\x5B\x81" + a[:4]), [])
        self.assertEqual(parser.feed(a[4:]), [(7, 0, 8)])
        bad = bytearray(a)
        bad[-1] ^= 1
        self.assertEqual(parser.feed(bytes(bad)), [])


class SessionTests(unittest.TestCase):
    def test_clean_transfer(self):
        data, meter = image(), DemoSdReceiver()
        s = U.UploadSession(data)
        run(s, meter)
        self.assertTrue(s.ok, s.message)
        self.assertEqual(meter.card, data)
        self.assertIn("MEM + HOLD", s.message)

    def test_damaged_lost_and_skipped_packets(self):
        data, meter = image(5000), DemoSdReceiver()

        def mangle(n, p):
            if n == 4:
                return p[:-1] + bytes([p[-1] ^ 0xFF])     # CRC wrong: status 1, sent again
            if n in (7, 8):
                return None                               # lost twice: timeouts, sent again
            return p

        s = U.UploadSession(data)
        run(s, meter, mangle)
        self.assertTrue(s.ok, s.message)
        self.assertEqual(meter.card, data)

    def test_meter_names_the_packet_it_expects(self):
        s = U.UploadSession(image(3000))
        s.answer(0, 0, 0)                                 # START
        s.answer(0, 0, 1)
        s.answer(1, 0, 2)
        s.answer(2, 2, 1)                                 # the meter wants packet 1 again
        self.assertEqual(s.step().seq, 1)
        self.assertFalse(s.answer(5, 0, 6))               # a late answer to another packet is ignored

    def test_no_meter_on_page_ld(self):
        s = U.UploadSession(image())
        while s.timeout():
            pass
        self.assertTrue(s.done and not s.ok)
        self.assertIn("Ld", s.message)

    def test_end_errors(self):
        for status, words in ((0x13, "not a firmware"), (0x14, "do NOT start"), (0x15, "do NOT start"),
                              (0x11, "FatFs result 7")):
            s = U.UploadSession(image(464))             # two DATA packets
            s.answer(0, 0, 0)
            s.answer(0, 0, 1)
            s.answer(1, 0, 2)
            self.assertEqual(s.phase, "end")
            s.answer(2, status, 0x0307)
            self.assertTrue(s.done and not s.ok)
            self.assertIn(words, s.message)

    def test_meter_refuses_other_files(self):
        meter = DemoSdReceiver()
        s = U.UploadSession(image(sp=0))                  # the app checks first; the meter checks again
        run(s, meter)
        self.assertFalse(s.ok)
        self.assertIn("not a firmware", s.message)
        self.assertIsNone(meter.card)


class FakeLink(MeterLink):
    def __init__(self) -> None:
        super().__init__(settings=None)
        self.is_connected = True
        self.meter = DemoSdReceiver()
        self.written: list[bytes] = []

    def write_frame(self, payload: bytes, done=None) -> None:
        self.written.append(bytes(payload))
        if done:
            done(None)
        answer = self.meter.packet(bytes(payload))
        if answer:
            self.ingest(answer[:5])                       # answers may arrive split
            self.ingest(answer[5:])


class LinkTests(unittest.TestCase):
    def test_link_drives_the_upload(self):
        link, data = FakeLink(), image(3000)
        progress, result = [], []
        link.upload_firmware(data, lambda f, *_: progress.append(f), lambda ok, t: result.append((ok, t)))
        self.assertEqual(result and result[0][0], True, result)
        self.assertEqual(link.meter.card, data)
        self.assertFalse(link.uploading)
        self.assertAlmostEqual(progress[-1], 1.0)

    def test_cancel_sends_abort(self):
        link = FakeLink()
        link.write_frame = lambda payload, done=None: link.written.append(bytes(payload))   # no answers
        result = []
        link.upload_firmware(image(), lambda *_: None, lambda ok, t: result.append((ok, t)))
        link.cancel_upload()
        self.assertEqual(result, [(False, "Upload cancelled")])
        self.assertEqual(link.written[-1][2], U.T_ABORT)
        self.assertFalse(link.uploading)

    def test_watchdog_quiet_on_page_ld(self):
        link = FakeLink()
        link._last_packet_at = 0.0                        # readings stopped long ago
        link.upload_mode = True
        link._watchdog_tick()
        self.assertFalse(link.communication_lost)
        link.upload_mode = False
        link._watchdog_tick()
        self.assertTrue(link.communication_lost)


if __name__ == "__main__":
    unittest.main()
