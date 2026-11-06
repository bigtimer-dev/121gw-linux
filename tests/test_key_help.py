import unittest

from gw121.keys import MeterKey, key_help
from gw121.packet import MeterMode as M


class KeyHelpTests(unittest.TestCase):
    def test_every_key_has_a_press_text(self):
        for key in MeterKey:
            for mode in (None, *[m.value for m in M if m != M.UNKNOWN]):
                for setup in (False, True):
                    press, hold = key_help(key, mode, setup)
                    self.assertTrue(press, (key, mode, setup))
                    self.assertTrue(hold is None or hold, (key, mode, setup))

    def test_follows_the_function(self):
        self.assertIn("15 V", key_help(MeterKey.RANGE, M.DIODE)[0])
        self.assertIn("zero offset", key_help(MeterKey.REL, M.DC_MA)[1])
        self.assertIn("1 kHz", key_help(MeterKey.REL, M.ACV)[1])
        self.assertIsNone(key_help(MeterKey.REL, M.DCV)[1])
        self.assertNotIn("auto-hold", key_help(MeterKey.HOLD, M.CAPACITOR)[0])
        self.assertIn("auto-hold", key_help(MeterKey.HOLD, M.DCV)[0])

    def test_peak_hold_always_warns_about_bluetooth(self):
        for mode in (None, M.DCV, M.ACV):
            for setup in (False, True):
                self.assertIn("disconnects", key_help(MeterKey.PEAK, mode, setup)[1])

    def test_setup_arrows(self):
        self.assertTrue(key_help(MeterKey.REL, M.DCV, True)[0].startswith("▲"))
        self.assertTrue(key_help(MeterKey.PEAK, M.DCV, True)[0].startswith("▼"))


if __name__ == "__main__":
    unittest.main()
