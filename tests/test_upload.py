import struct
import time
import unittest

from gi.repository import GLib

from gw121 import upload as U
from gw121.demo import DemoSdReceiver
from gw121.link import MeterLink
from gw121.packet import build_frame


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


class RepairTests(unittest.TestCase):
    """Answers as a real 121GW's Bluetooth module damaged them at a 7.5 ms interval (btmon, 2026-11-05)."""

    DAMAGED = [
        ("b5 2b 81 01 00 03 00 02 00 48 5a", 1, (1, 0, 2)),       # 5B -> 2B
        ("b5 56 81 03 00 03 00 04 00 ae 7b", 3, (3, 0, 4)),       # 5B -> 56
        ("b5 5b 60 15 00 03 00 16 00 da 89", 21, (21, 0, 22)),    # 81 -> 60
        ("b5 5b 81 10 00 03 00 21 00 55 79", 32, (32, 0, 33)),    # packet number 0x20 -> 0x10
        ("b5 5b 81 07 80 03 00 08 00 62 38", 7, (7, 0, 8)),       # high byte 00 -> 80
    ]

    def test_real_damaged_answers(self):
        for hexa, expect, want in self.DAMAGED:
            raw = bytes.fromhex(hexa)
            self.assertEqual(U.AnswerParser().feed(raw), [], hexa)               # the strict parser drops them
            self.assertEqual(U.repair_answer(raw, [expect]), want, hexa)

    def test_no_repair_without_proof(self):
        raw = bytearray.fromhex("b5 2b 81 01 00 03 00 02 00 48 5a")
        raw[8] ^= 1                                                              # "next" damaged too: CRC fails
        self.assertIsNone(U.repair_answer(bytes(raw), [1]))
        self.assertIsNone(U.repair_answer(bytes.fromhex("f2 17 80 00 00 09 86 fd f2 64 01 01 37 01 19"), [5]))

    def test_link_takes_a_repaired_answer(self):
        link, data = FakeLink(), image(3000)
        n = [0]
        orig = link.meter.packet

        def damaging(p):
            a = orig(p)
            n[0] += 1
            if a and n[0] % 3 == 0:
                a = a[:1] + bytes([0x2B]) + a[2:]                                # every third answer damaged
            return a
        link.meter.packet = damaging
        result = []
        link.upload_firmware(data, lambda *_: None, lambda ok, t: result.append((ok, t)))
        self.assertEqual(result and result[0][0], True, result)
        self.assertEqual(link.meter.card, data)
        self.assertGreater(link.answers_repaired, 3)


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


def spin(seconds: float) -> None:
    ctx, end = GLib.MainContext.default(), time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


class PieceLink(FakeLink):
    """Takes at most 20 bytes per write; the meter side joins the bytes into packets as the firmware does."""

    def __init__(self) -> None:
        super().__init__()
        self.rx = b""

    def write_piece_size(self) -> int:
        return 20

    def write_frame(self, payload: bytes, done=None) -> None:
        assert len(payload) <= 20, len(payload)
        self.written.append(bytes(payload))
        self.rx += payload
        if done:
            done(None)
        if len(self.rx) >= 6 and len(self.rx) >= 8 + self.rx[5]:
            n = 8 + self.rx[5]
            p, self.rx = self.rx[:n], self.rx[n:]
            answer = self.meter.packet(p)
            if answer:
                self.ingest(answer)


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
        self.assertNotEqual(link.written[-1][2], U.T_ABORT)       # not at once: the meter first drops a half packet
        spin(0.5)
        self.assertEqual(link.written[-1][2], U.T_ABORT)
        self.assertFalse(link.uploading)

    def test_pieces_of_20_bytes(self):
        """A link that takes 20 bytes per write (ATT MTU 23, the 121GW): the packets go in pieces, the meter joins them."""
        link, data = PieceLink(), image(3000)
        result = []
        link.upload_firmware(data, lambda *_: None, lambda ok, t: result.append((ok, t)))
        self.assertEqual(result and result[0][0], True, result)
        self.assertEqual(link.meter.card, data)
        self.assertLessEqual(max(map(len, link.written)), 20)
        self.assertGreater(len(link.written), 13 * 12)       # 13 DATA packets of 240 bytes, 12 pieces each

    def test_cancel_stops_the_pieces(self):
        link = PieceLink()
        held = []
        link.write_frame = lambda payload, done=None: (link.written.append(bytes(payload)), held.append(done))
        link.upload_firmware(image(), lambda *_: None, lambda *_: None)
        self.assertEqual(len(link.written), 1)                # START (17 bytes) went in one piece
        link.cancel_upload()
        held[-1](None)                                        # the write of the last piece finishes after the cancel
        spin(0.5)
        self.assertEqual([p[2] for p in link.written if len(p) > 2 and p[:2] == b"\xB5\x5B"][-1], U.T_ABORT)

    def test_probe_finds_page_ld(self):
        link = FakeLink()                                 # its meter answers transfer packets: it shows the page Ld
        link._last_packet_at = time.monotonic() - 3       # readings stopped 3 s ago
        link._watchdog_tick()
        self.assertTrue(link.on_ld_page)
        self.assertEqual(link.written[-1][2], U.T_STATUS)
        self.assertFalse(link.communication_lost)
        link.ingest(build_frame(1, 100, 1))               # readings again: the page was left
        self.assertFalse(link.on_ld_page)

    def test_probe_unanswered_is_lost(self):
        link = FakeLink()
        link.write_frame = lambda payload, done=None: link.written.append(bytes(payload))   # nobody answers
        link._last_packet_at = time.monotonic() - 6
        link._watchdog_tick()
        self.assertEqual(link.written[-1][2], U.T_STATUS)
        self.assertFalse(link.on_ld_page)
        self.assertTrue(link.communication_lost)

    def test_no_probe_while_readings_flow(self):
        link = FakeLink()
        link._last_packet_at = time.monotonic()
        link._watchdog_tick()
        self.assertEqual(link.written, [])

    def test_watchdog_quiet_on_page_ld(self):
        link = FakeLink()
        link.write_frame = lambda payload, done=None: link.written.append(bytes(payload))   # no probe answers
        link._last_packet_at = 0.0                        # readings stopped long ago
        link.upload_mode = True
        link._watchdog_tick()
        self.assertFalse(link.communication_lost)
        link.upload_mode = False
        link._watchdog_tick()
        self.assertTrue(link.communication_lost)


if __name__ == "__main__":
    unittest.main()
