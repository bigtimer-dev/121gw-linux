import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from gw121 import card
from gw121.demo import DemoCard


class Meter:
    """DemoCard behind a request function, with optional lost answers."""

    def __init__(self, drop=()):
        self.dev = DemoCard()
        self.seq = 0
        self.n = 0
        self.drop = set(drop)
        self.parser = card.ReplyParser()

    def request(self, rtype, payload=b""):
        self.seq += 1
        self.n += 1
        ans = self.dev.handle(rtype, self.seq, payload)
        if self.n in self.drop:
            raise card.CardError("no answer")
        got = []
        for i in range(0, len(ans), 20):            # arrives in pieces of 20 bytes
            got += self.parser.feed(ans[i:i + 20])
        assert len(got) == 1 and got[0][0] == self.seq
        return got[0][1:]


class CardTests(unittest.TestCase):
    def test_list_and_info(self):
        c = card.CardClient(Meter().request)
        names = [f.name for f in c.files()]
        self.assertIn("LOG0002.CSV", names)
        self.assertTrue(next(f for f in c.files() if f.name == "cal.bin").protected)
        free, total = c.info()
        self.assertGreater(total, free)

    def test_download_checks_crc(self):
        m = Meter(drop={5, 9})
        c = card.CardClient(m.request)
        f = next(x for x in c.files() if x.name == "LOG0002.CSV")
        with tempfile.TemporaryDirectory() as d:
            p = c.download(f, Path(d))
            self.assertEqual(p.read_bytes(), m.dev.files["LOG0002.CSV"][0])
            self.assertFalse((Path(d) / "LOG0002.CSV.part").exists())

    def test_sync_copies_only_new(self):
        m = Meter()
        c = card.CardClient(m.request)
        with tempfile.TemporaryDirectory() as d:
            r = c.sync(Path(d))
            self.assertIn("LOG0001.CSV", r["copied"])
            self.assertNotIn("btload.tmp", [x.lower() for x in r["copied"]])
            r2 = c.sync(Path(d))
            self.assertEqual(r2["copied"], [])
            self.assertEqual(r2["unchanged"], len(r["copied"]))

    def test_delete(self):
        m = Meter()
        c = card.CardClient(m.request)
        c.delete("LOG0003.CSV")
        self.assertNotIn("LOG0003.CSV", m.dev.files)
        with self.assertRaises(card.CardError):
            c.delete("cal.bin")
        with self.assertRaises(card.CardError):
            c.delete("../x")

    def test_delete_lost_answer_counts_as_done(self):
        m = Meter()
        c = card.CardClient(m.request)
        orig = m.request
        calls = []

        def lossy(t, p=b""):
            r = orig(t, p)
            calls.append(t)
            if len(calls) == 1:
                raise card.CardError("no answer")          # deleted, but the answer was lost
            return r
        c._request = lossy
        c.delete("LOG0001.CSV")
        self.assertEqual(len(calls), 2)

    def test_settings_shared_byte(self):
        m = Meter()
        c = card.CardClient(m.request)
        raw = c.get_settings()
        raw[0x020] = 0b1001                                 # REL percent, trigger interval, 12 hour
        s = card.SETTINGS_BY_ID[0x220]
        b = card.wire_value(s, 2, raw)
        self.assertEqual(b, 0b1101)
        self.assertEqual(card.settings_values({0x020: b})[0x120], 1)
        self.assertEqual(card.settings_values({0x020: b})[0x320], 1)
        self.assertEqual(c.put_setting(0x020, b), b)
        with self.assertRaises(card.CardError):
            card.wire_value(card.SETTINGS_BY_ID[0x01A], 30, raw)

    def test_repair_damaged_answer(self):
        ans = DemoCard.reply(0x4005, 0, 3, b"abc")
        bad = bytearray(ans)
        bad[1] = 0x2B                                       # 5B -> 2B, as the module does
        bad[3] = 0x15                                       # seq low byte slipped
        self.assertEqual(card.ReplyParser().feed(bytes(bad)), [])
        self.assertEqual(card.repair_reply(bytes(bad), 0x4005), (0x4005, 0, 3, b"abc"))
        self.assertIsNone(card.repair_reply(bytes(bad), 0x4006))

    def test_names(self):
        for bad in ("", "a" * 64, "x/y", "a..b", " a", "a."):
            with self.assertRaises(card.CardError):
                card.check_name(bad)
        card.check_name("LOG 0001.CSV")

    def test_fat_time(self):
        f = card.CardFile("a", 1, (46 << 9) | (11 << 5) | 5, (14 << 11) | (30 << 5) | 3, 0x20)
        self.assertEqual(str(f.when), "2026-11-05 14:30:06")

    def test_needs_newer(self):
        from gw121.cardview import needs_newer
        self.assertIsNotNone(needs_newer("C-1.04"))
        self.assertIsNone(needs_newer("C-1.06"))
        self.assertIsNotNone(needs_newer("F-1.04"))
        self.assertIsNone(needs_newer("F-1.05"))
        self.assertIsNone(needs_newer(None))


if __name__ == "__main__":
    unittest.main()


class DamagedAnswerTests(unittest.TestCase):
    """A damaged card answer is asked again at once (after CARD_GAP_MS), not after the request's timeout."""

    def test_retry_after_damaged_answer(self):
        import threading
        import time
        from gi.repository import GLib
        from gw121 import demo
        from gw121.settings import Settings
        link = demo.DemoLink(Settings(Path(tempfile.mkdtemp()) / "s.json"))
        link.settings.auto_reconnect = False
        link.start()
        link.connect_device(demo.DEMO_ADDRESS)
        orig = demo.DemoCard.reply
        sent = []

        def damaged_once(seq, status, field=0, data=b""):
            a = bytearray(orig(seq, status, field, data))
            sent.append(seq)
            if len(sent) == 1:
                a[12] ^= 0x10                               # one bit inside the data, as the module does
            return bytes(a)
        demo.DemoCard.reply = staticmethod(damaged_once)
        out = {}
        try:
            client = card.CardClient(link.card_request)
            th = threading.Thread(target=lambda: out.setdefault("files", client.files()))
            t0 = time.monotonic()
            th.start()
            ctx = GLib.MainContext.default()
            while th.is_alive() and time.monotonic() - t0 < 10:
                ctx.iteration(False)
                time.sleep(0.005)
            took = time.monotonic() - t0
        finally:
            demo.DemoCard.reply = staticmethod(orig)
            link.disconnect()
        self.assertIn("LOG0001.CSV", [f.name for f in out.get("files", [])])
        self.assertEqual(len(sent), 2)                      # the damaged answer and the second, good one
        self.assertLess(took, 2.0)                          # not the 15 s timeout of LIST


class CombineCopiesTests(unittest.TestCase):
    def _copies(self, data, damage):
        out = []
        for k, (pos, val) in enumerate(damage):
            a = bytearray(DemoCard.reply(0x4100 + k, 0, 0, data))
            if pos is not None:
                a[pos] = val
            out.append(bytes(a))
        return out

    def test_two_copies_damaged_at_different_places(self):
        data = bytes(range(100))
        frames = self._copies(data, [(22, 0x01), (31, 0x27)])
        self.assertEqual(card.ReplyParser().feed(frames[0] + frames[1]), [])
        got = card.combine_copies(frames)
        self.assertIsNotNone(got)
        self.assertEqual(got[3], data)
        self.assertEqual(got[1:3], (0, 0))

    def test_same_place_needs_a_third_copy(self):
        data = bytes(range(60))
        frames = self._copies(data, [(15, 0x00), (15, 0xFF)])
        self.assertIsNone(card.combine_copies(frames))
        frames += self._copies(data, [(20, 0x55)])
        frames[2] = frames[2]                               # third copy, damaged elsewhere
        got = card.combine_copies(frames)
        self.assertEqual(got[3], data)

    def test_lost_bytes_are_not_mixed(self):
        data = bytes(range(100))
        a, b = self._copies(data, [(22, 0x01), (None, 0)])
        self.assertIsNone(card.combine_copies([a[:-30], b[:-25]]))

    def test_client_combines(self):
        m = Meter()
        import itertools
        damage = itertools.cycle([12, 18, 25, 14, 30, 11, 16, 27, 13, 19, 17])
        orig = m.request

        def lossy(t, p=b""):
            m.seq += 1
            ans = bytearray(m.dev.handle(t, m.seq, p))
            if len(ans) > 30:
                ans[next(damage)] ^= 0x5A                      # every long answer damaged, as measured
                raise card.CardError("damaged", raw=bytes(ans))
            return card.ReplyParser().feed(bytes(ans))[0][1:]
        c = card.CardClient(lossy)
        f = next(x for x in c.files() if x.name == "LOG0002.CSV")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(c.download(f, Path(d)).read_bytes(), m.dev.files["LOG0002.CSV"][0])
        self.assertGreater(c.combined, 10)


class StreamMeter(Meter):
    """Meter with STREAM (C-1.07 on); `lose` = piece indexes lost on their first sending."""

    def __init__(self, lose=(), fast=True):
        super().__init__()
        self.lose = set(lose)
        self.dev.fast = fast
        self.streams = 0
        self.pace = 13
        self.lose_from_pace = False

    def request(self, rtype, payload=b""):
        if rtype == card.T_LINK:
            self.pace = payload[0]
        return super().request(rtype, payload)

    def stream(self, first, count, size):
        self.streams += 1
        if self.lose_from_pace and self.pace > 6:          # module overrun: every other piece lost
            self.lose |= set(range(first, first + count, 2))
        self.seq += 1
        if not self.dev.fast:
            return card.ST_NOT_NOW, 0, {}                   # older firmware: a 0x81 answer, status 3
        out, status, field = {}, None, 0
        for item in card.StreamParser().feed(self.dev.handle(card.T_STREAM, self.seq, card.stream_payload(first, count, size))):
            if item[0] == "answer":
                status, field = item[2], item[3]
            elif item[1] in self.lose:
                self.lose.discard(item[1])
            else:
                out[item[1]] = item[2]
        return status, field, out


class StreamTests(unittest.TestCase):
    def _download(self, m, name="EEVBlog.bin"):
        c = card.CardClient(m.request, stream=m.stream)
        f = next(x for x in c.files() if x.name == name)
        with tempfile.TemporaryDirectory() as d:
            got = c.download(f, Path(d)).read_bytes()
        self.assertEqual(got, m.dev.files[name][0])
        return c

    def test_stream_download(self):
        m = StreamMeter()
        m.dev.files["EEVBlog.bin"] = (bytes(i * 7 & 0xFF for i in range(170000)), 0, 0)
        c = self._download(m)
        self.assertTrue(c.fast)
        self.assertEqual(c.streamed, sum(-(-min(8192, 170000 - o) // 232) for o in range(0, 170000, 8192)))
        self.assertLess(m.n, 40)                            # READs and LISTs, no CHUNK

    def test_lost_pieces_streamed_again(self):
        m = StreamMeter(lose={0, 5, 6, 7, 35})
        c = self._download(m, "LOG0002.CSV")
        self.assertTrue(c.fast)
        self.assertGreater(m.streams, 1)

    def test_slower_pace_when_pieces_get_lost(self):
        m = StreamMeter()
        m.lose_from_pace = True
        c = self._download(m, "LOG0002.CSV")
        self.assertEqual((c.pace, m.pace), (6, 6))
        self.assertTrue(c.fast)

    def test_older_firmware_falls_back(self):
        m = StreamMeter(fast=False)
        c = self._download(m, "LOG0002.CSV")
        self.assertFalse(c.fast)
        self.assertEqual(m.streams, 1)                       # asked once, then CHUNKs

    def test_parser(self):
        d = DemoCard()
        d.staged = bytes(range(256)) * 2
        raw = d.handle(card.T_STREAM, 0x4001, card.stream_payload(0, 9, 100))
        items = []
        p = card.StreamParser()
        for i in range(0, len(raw), 20):
            items += p.feed(raw[i:i + 20])
        self.assertEqual(items[0][:4], ("answer", 0x4001, 0, 6))
        self.assertEqual(b"".join(x[2] for x in items[1:]), d.staged)
        old = card.StreamParser().feed(bytes.fromhex("b55b81") + b"\x05\x00\x03\x03\x00\x00" + b"\x00\x00")
        self.assertEqual(old, [])                           # a bad CRC is skipped

    def test_through_link(self):
        import threading
        import time
        from gi.repository import GLib
        from gw121 import demo
        from gw121.settings import Settings
        link = demo.DemoLink(Settings(Path(tempfile.mkdtemp()) / "s.json"))
        link.settings.auto_reconnect = False
        link.start()
        link.connect_device(demo.DEMO_ADDRESS)
        out = {}
        try:
            client = card.CardClient(link.card_request, stream=link.card_stream)

            def job():
                f = next(x for x in client.files() if x.name == "LOG0002.CSV")
                with tempfile.TemporaryDirectory() as d:
                    out["data"] = client.download(f, Path(d)).read_bytes()
            th = threading.Thread(target=job, daemon=True)
            th.start()
            ctx = GLib.MainContext.default()
            t0 = time.monotonic()
            while th.is_alive() and time.monotonic() - t0 < 20:
                ctx.iteration(False)
                time.sleep(0.002)
            want = link._receiver.files.files["LOG0002.CSV"][0]
        finally:
            link.disconnect()
        self.assertEqual(out.get("data"), want)
        self.assertTrue(client.fast)
        self.assertGreater(client.streamed, 0)
