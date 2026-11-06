import unittest

from gw121 import card
from gw121.cardview import format_refused


def client(answer):
    sent = []

    def request(t, p):
        sent.append((t, p))
        if isinstance(answer, Exception):
            raise answer
        return answer
    return card.CardClient(request), sent


class FormatTests(unittest.TestCase):
    def test_sends_format_once(self):
        c, sent = client((0, 0, b""))
        c.format()
        self.assertEqual(sent, [(0x15, b"FORMAT")])

    def test_old_firmware_refuses(self):
        c, _ = client((3, 0, b""))
        with self.assertRaisesRegex(card.CardError, "F-1.09 / C-1.08"):
            c.format()

    def test_damaged_answer_means_finished(self):
        c, sent = client(card.CardError("damaged", raw=b"\xB5\x5B\x82"))
        c.format()
        self.assertEqual(len(sent), 1)

    def test_no_answer(self):
        c, sent = client(card.CardError("no answer"))
        with self.assertRaisesRegex(card.CardError, "did not answer"):
            c.format()
        self.assertEqual(len(sent), 1)          # never sent twice

    def test_long_timeout(self):
        self.assertGreaterEqual(card.TIMEOUT_MS[card.T_FORMAT], 120_000)

    def test_which_meters_can(self):
        self.assertIsNone(format_refused("C-1.08", "B-1.12"))
        self.assertIsNone(format_refused("F-1.12", None))
        self.assertIsNotNone(format_refused("C-1.07", "B-1.12"))
        self.assertIsNotNone(format_refused("F-1.08", "B-1.12"))
        self.assertIsNotNone(format_refused("C-1.08", "B-EEV"))

    def test_demo_meter_formats(self):
        from gw121.demo import DemoCard
        d = DemoCard()
        self.assertTrue(d.files)
        out = d.handle(0x15, 7, b"FORMAT")
        self.assertEqual(out[2], 0x82)
        self.assertFalse(d.files)
        self.assertEqual(d.handle(0x15, 8, b"FORMAX")[6], 0x22)


if __name__ == "__main__":
    unittest.main()
