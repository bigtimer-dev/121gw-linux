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
