"""Direct firmware update through zombu4's two-part bootloader: BootSession and the link's flow, against a model of
bootloader/src/bl_ble.s (update mode) and firmware/src/ble/blcmd.s (key codes 0x91 / 0x92)."""
import struct
import time
import unittest
import zlib

from gi.repository import GLib

from gw121 import upload as U
from gw121.link import MeterLink
from gw121.packet import build_frame

from test_upload import image, spin


def answer(seq, status, nxt):
    body = struct.pack("<BHBBH", 0x81, seq & 0xFFFF, 3, status, nxt & 0xFFFF)
    return b"\xB5\x5B" + body + struct.pack("<H", U.crc16(body))


def f7(text: bytes) -> bytes:
    x = 0xF7
    for b in text:
        x ^= b
    return b"\xF7" + text + bytes([x])


class FakeBootloader:
    """bl_ble.s: STATUS 0x80 -> version, START erases, DATA in order (earlier: status 0, ahead: status 2), END checks."""

    VERSION = 0x111                     # B-1.11

    def __init__(self):
        self.state, self.flash, self.expect, self.size, self.crc = "wait", None, 0, 0, 0
        self.data = bytearray()
        self.restarted = False

    def packet(self, p: bytes):
        if len(p) < 8 or len(p) != 8 + p[5]:
            return None
        body = p[2:-2]
        ptype, seq, n = struct.unpack_from("<BHB", body)
        pl = body[4:]
        if struct.unpack("<H", p[-2:])[0] != U.crc16(body):
            return answer(seq, 1, self.expect)
        if ptype == U.T_STATUS:
            return answer(seq, 0, self.VERSION) if pl == b"\x80" else answer(seq, 3, 0)
        if ptype == U.T_START:
            if len(pl) not in (9, 13) or pl[8] != 0:
                return answer(seq, 4, 0)
            self.size, self.crc = struct.unpack_from("<II", pl)
            self.flash, self.data, self.expect, self.state = None, bytearray(), 0, "recv"
            return answer(seq, 0, 0)
        if ptype == U.T_DATA:
            if self.state != "recv":
                return answer(seq, 3, 0)
            if seq < self.expect:
                return answer(seq, 0, self.expect)
            if seq > self.expect:
                return answer(seq, 2, self.expect)
            self.data += pl
            self.expect += 1
            return answer(seq, 0, self.expect)
        if ptype == U.T_END:
            if self.state != "recv":
                return answer(seq, 3, 0)
            if len(self.data) != self.size or zlib.crc32(self.data) & 0xFFFFFFFF != self.crc:
                return answer(seq, 0x12, 0)
            self.flash, self.state, self.restarted = bytes(self.data), "done", True
            return answer(seq, 0, 0)
        if ptype == U.T_ABORT:
            self.state = "wait"
            return answer(seq, 0, 0)
        return answer(seq, 3, 0)


def run(session, meter, lose=lambda n, p: False, limit=100_000):
    """Drive a BootSession; packets are delivered in order, lose(n, p) drops the n-th packet written."""
    parser, n = U.AnswerParser(), 0
    for _ in range(limit):
        if session.done:
            return
        out = session.to_send()
        if not out:
            if not session.timeout():
                return
            continue
        for p in out:
            n += 1
            if lose(n, p):
                continue
            a = meter.packet(p)
            for got in parser.feed(a or b""):
                session.answer(*got)
    raise AssertionError("did not finish")


class BootSessionTests(unittest.TestCase):
    def test_parse_bootloader_answer(self):
        self.assertEqual(U.parse_bootloader_answer(b"\xF4\x39\x32\x39\x32" + f7(b"B-111")), "B-1.11")
        self.assertEqual(U.parse_bootloader_answer(f7(b"B-EEV")), "B-EEV")
        bad = bytearray(f7(b"B-111"))
        bad[-1] ^= 1
        self.assertIsNone(U.parse_bootloader_answer(bytes(bad)))
        self.assertIsNone(U.parse_bootloader_answer(f7(b"F-104")))
        self.assertTrue(U.has_direct_update("B-1.11"))
        self.assertFalse(U.has_direct_update("B-EEV"))
        self.assertFalse(U.has_direct_update(None))

    def test_probe_tells_update_mode_from_page_ld(self):
        self.assertEqual(U.probe_packet(1)[6], 0x80)
        self.assertEqual(U.probe_result(0, 0x111), "B-1.11")
        self.assertIsNone(U.probe_result(3, 0))

    def test_clean_transfer_two_in_flight(self):
        data, meter = image(5000), FakeBootloader()
        s = U.BootSession(data)
        first = s.to_send()
        self.assertEqual(first[0][2], U.T_START)
        self.assertEqual(first[0][5], 9)                  # size, CRC-32, kind 0
        meter.packet(first[0])
        s.answer(0, 0, 0)
        self.assertEqual([p[2] for p in s.to_send()], [U.T_DATA, U.T_DATA])    # two in flight
        self.assertEqual(s.to_send(), [])
        s2 = U.BootSession(data)
        run(s2, FakeBootloader())
        self.assertTrue(s2.ok, s2.message)
        m = FakeBootloader()
        run(U.BootSession(data), m)
        self.assertEqual(m.flash, data)
        self.assertEqual(s2.resends, 0)

    def test_lost_packets_and_answers(self):
        data = image(20000)
        for every in (3, 7, 11):
            m = FakeBootloader()
            s = U.BootSession(data)
            run(s, m, lose=lambda n, p, e=every: n % e == 0 and p[2] == U.T_DATA)
            self.assertTrue(s.ok, (every, s.message))
            self.assertEqual(m.flash, data)

    def test_damaged_packet(self):
        data, m = image(3000), FakeBootloader()
        s = U.BootSession(data)
        hit = []

        class Mangle(FakeBootloader):
            def packet(self, p):
                if p[2] == U.T_DATA and p[3] == 4 and not hit:
                    hit.append(1)
                    p = p[:-1] + bytes([p[-1] ^ 1])
                return m.packet(p)

        run(s, Mangle())
        self.assertTrue(s.ok, s.message)
        self.assertEqual(m.flash, data)

    def test_end_error_and_refused_start(self):
        s = U.BootSession(image(3000))
        s.to_send()
        s.answer(0, 4, 0)
        self.assertTrue(s.done)
        self.assertFalse(s.ok)
        self.assertIn("START", s.message)
        s = U.BootSession(image(500))
        s.to_send()
        s.answer(0, 0, 0)
        while s.phase == "data":
            for p in s.to_send():
                s.answer(int.from_bytes(p[3:5], "little"), 0, int.from_bytes(p[3:5], "little") + 1)
        self.assertEqual(s.to_send()[0][2], U.T_END)
        s.answer(len(s.chunks), 0x12, 0)
        self.assertFalse(s.ok)
        self.assertIn("update mode", s.message)


class BootLink(MeterLink):
    """A meter on firmware v1.0.4+ with zombu4's bootloader. Writes of up to 20 bytes; the meter's answers and the
    write confirmations come from the main loop, as over BlueZ."""

    def __init__(self, bootloader=b"B-111"):
        super().__init__(settings=None)
        self.is_connected = True
        self.mode = "firmware"
        self.boot = FakeBootloader()
        self.bl_answer = bootloader
        self.rx = b""
        self.written = []
        self.lose = lambda p: False
        self.restarts = 0

    def write_piece_size(self):
        return 20

    def set_auto_reconnect(self, value):
        pass

    def write_frame(self, payload, done=None):
        assert len(payload) <= 20
        self.written.append(bytes(payload))
        if done:
            GLib.idle_add(lambda: (done(None), GLib.SOURCE_REMOVE)[1])
        if self.mode == "firmware":
            if payload == U.BOOTLOADER_INFO_REQUEST:
                self._later(payload + f7(self.bl_answer))
            elif payload == U.UPDATE_MODE_REQUEST and self.bl_answer != b"B-EEV":
                self._later(payload)
                GLib.timeout_add(50, self._restart, "boot")
            return
        self.rx += payload
        while len(self.rx) >= 6 and len(self.rx) >= 8 + self.rx[5]:
            n = 8 + self.rx[5]
            p, self.rx = self.rx[:n], self.rx[n:]
            if self.lose(p):
                continue
            a = self.boot.packet(p)
            if a:
                self._later(a)
            if self.boot.restarted:
                self.boot.restarted = False
                GLib.timeout_add(100, self._restart, "firmware")

    def _later(self, data):
        GLib.idle_add(lambda: (self.ingest(data), GLib.SOURCE_REMOVE)[1])

    def _restart(self, mode):
        self.restarts += 1
        self._reset_link_state()                       # the link drops with the meter's restart
        self.mode, self.rx = mode, b""
        GLib.timeout_add(100, self._reconnect)
        return GLib.SOURCE_REMOVE

    def _reconnect(self):
        self.is_connected = True
        self._start_watchdog()
        self._connected_at = time.monotonic() - 1
        return GLib.SOURCE_REMOVE


def wait(cond, seconds=20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not cond():
        spin(0.02)
    return cond()


class BootLinkTests(unittest.TestCase):
    def stream(self, link, n=5):
        for i in range(n):
            link.ingest(build_frame(1, 100 + i, 1))
            spin(0.01)

    def test_bootloader_is_asked_on_v104(self):
        link = BootLink()
        link.firmware_version = "C-1.04"
        self.stream(link)
        self.assertTrue(wait(lambda: link.bootloader == "B-1.11", 2))
        self.assertTrue(link.direct_update)
        old = BootLink()
        old.firmware_version = "C-1.03"
        self.stream(old)
        self.assertNotIn(U.BOOTLOADER_INFO_REQUEST, old.written)
        eev = BootLink(b"B-EEV")
        eev.firmware_version = "F-1.04"
        self.stream(eev)
        self.assertTrue(wait(lambda: eev.bootloader == "B-EEV", 2))
        self.assertFalse(eev.direct_update)

    def test_version_asked_again_after_a_reconnect(self):
        link = BootLink()
        self.stream(link, 20)                       # a long first connection
        link._reset_link_state()                    # the meter restarts (new firmware), the link comes back
        link.is_connected = True
        link.written.clear()
        self.stream(link, 3)
        from gw121.keys import VERSION_REQUEST
        self.assertIn(VERSION_REQUEST, link.written)

    def test_full_update_from_the_firmware(self):
        link, data = BootLink(), image(6000)
        link._start_watchdog()
        states, progress, result = [], [], []
        link.install_firmware(data, lambda f, *_: progress.append(f), lambda ok, t: result.append((ok, t)),
                              states.append)
        self.assertIn(U.UPDATE_MODE_REQUEST, link.written)
        self.assertTrue(wait(lambda: result, 30), states)
        self.assertTrue(result[0][0], result)
        self.assertEqual(link.boot.flash, data)
        self.assertAlmostEqual(progress[-1], 1.0)
        self.assertTrue(any("erasing" in s for s in states), states)
        self.assertFalse(link.uploading)
        self.assertTrue(wait(lambda: link.restarts == 2, 3))       # into the bootloader, back into the firmware
        link._stop_watchdog()

    def test_update_with_lost_packets(self):
        link, data = BootLink(), image(8000)
        n = [0]

        def lose(p):
            n[0] += 1
            return p[2] == U.T_DATA and n[0] % 5 == 0

        link.lose = lose
        link.mode = "boot"
        link.update_mode = "B-1.11"
        result = []
        link.install_firmware(data, lambda *_: None, lambda ok, t: result.append((ok, t)))
        self.assertTrue(wait(lambda: result, 60))
        self.assertTrue(result[0][0], result)
        self.assertEqual(link.boot.flash, data)

    def test_without_zombu4_bootloader_the_meter_keeps_measuring(self):
        link = BootLink(b"B-EEV")
        result = []
        link.install_firmware(image(3000), lambda *_: None, lambda ok, t: result.append((ok, t)))
        link.asked = time.monotonic()
        link._boot_job["asked_at"] -= 10                 # 0x91 went out 10 s ago, readings still come
        link.ingest(build_frame(1, 100, 1))
        self.assertEqual(result and result[0][0], False)
        self.assertIn("SD card", result[0][1])
        self.assertFalse(link.uploading)

    def test_update_mode_found_by_the_probe(self):
        link = BootLink()
        link.mode = "boot"
        link._connected_at = time.monotonic() - 3
        link._watchdog_tick()
        self.assertTrue(wait(lambda: link.update_mode == "B-1.11", 2))
        self.assertFalse(link.on_ld_page)
        self.assertFalse(link.communication_lost)

    def test_cancel_while_sending_aborts(self):
        link = BootLink()
        link.mode = "boot"
        link.update_mode = "B-1.11"
        result = []
        link.install_firmware(image(20000), lambda *_: None, lambda ok, t: result.append((ok, t)))
        wait(lambda: link.boot.expect > 3, 10)
        link.cancel_install()
        self.assertFalse(result[0][0])
        self.assertIn("update mode", result[0][1])
        wait(lambda: link.boot.state == "wait", 3)
        self.assertEqual(link.boot.state, "wait")


if __name__ == "__main__":
    unittest.main()


class BootloaderKindTests(unittest.TestCase):
    def test_start_payload_for_bootloader_image(self):
        import struct as _s
        s = U.BootSession(b"x" * 300, U.KIND_BOOTLOADER, 0x010102)
        pkt = s.to_send()[0]
        size, crc, kind, ver = _s.unpack_from("<IIBI", pkt, 6)
        self.assertEqual((size, kind, ver), (300, 2, 0x010102))
        self.assertEqual(len(U.BootSession(b"x" * 300).to_send()[0]), 8 + 9)
