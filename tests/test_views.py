import unittest

from gw121.packet import PacketV2, build_frame
from gw121.views import RTD_TABLE, view_for

DC_MA, OHM, DCV, TEMP_C = 19, 9, 1, 25


def pkt(mode, value, rng, **kw) -> PacketV2:
    return PacketV2(build_frame(mode, abs(value), rng, negative=value < 0, **kw))


def shown(p, loop="off", rtd="off", unit="°C"):
    v = view_for(p, loop, rtd, unit)
    return None if v is None else (("-" if v.negative else "") + v.text.strip(), v.unit)


class LoopViewTests(unittest.TestCase):
    # the vectors of the firmware's emulator test test_loop_percent.py
    def test_4_20(self):
        self.assertEqual(shown(pkt(DC_MA, 40000, 0), loop="4-20"), ("0.00", "%"))      # 4.0000 mA, range 0
        self.assertEqual(shown(pkt(DC_MA, 12000, 1), loop="4-20"), ("50.00", "%"))     # 12.000 mA, range 1
        self.assertEqual(shown(pkt(DC_MA, 20000, 1), loop="4-20"), ("100.00", "%"))
        self.assertEqual(shown(pkt(DC_MA, 0, 0), loop="4-20"), ("-25.00", "%"))
        self.assertEqual(shown(pkt(DC_MA, 55000, 1), loop="4-20"), ("318.75", "%"))

    def test_0_20_and_rounding_half_away_from_zero(self):
        self.assertEqual(shown(pkt(DC_MA, 10000, 1), loop="0-20"), ("50.00", "%"))
        self.assertEqual(shown(pkt(DC_MA, 40008, 0), loop="4-20"), ("0.01", "%"))      # +0.005 % -> 0.01
        self.assertEqual(shown(pkt(DC_MA, 39992, 0), loop="4-20"), ("-0.01", "%"))     # -0.005 % -> -0.01

    def test_not_shown(self):
        self.assertIsNone(shown(pkt(DC_MA, 12000, 1)))                                  # setting off
        self.assertIsNone(shown(pkt(DC_MA, 12000, 1, icon2=0x10), loop="4-20"))        # REL keeps its own display
        self.assertIsNone(shown(pkt(DC_MA, 12000, 1, overflow=True), loop="4-20"))     # overload: OL
        self.assertIsNone(shown(pkt(DC_MA, 12000, 1, icon3=0x40), loop="4-20"))        # calibration mode
        self.assertIsNone(shown(pkt(DCV, 12000, 1), loop="4-20"))                       # only DC mA


class RtdViewTests(unittest.TestCase):
    # the vectors of the firmware's emulator test test_rtd.py
    def test_table_is_the_firmware_table(self):
        self.assertEqual((len(RTD_TABLE), RTD_TABLE[0], RTD_TABLE[40], RTD_TABLE[-1]), (211, 185201, 1000000, 3904811))

    def test_pt100(self):
        self.assertEqual(shown(pkt(OHM, 10000, 1), rtd="pt100"), ("0.0", "°C"))         # 100.00 Ω
        self.assertEqual(shown(pkt(OHM, 13851, 1), rtd="pt100"), ("100.0", "°C"))
        self.assertEqual(shown(pkt(OHM, 18520, 0), rtd="pt100"), ("-200.0", "°C"))      # 18.520 Ω, range 0
        self.assertEqual(shown(pkt(OHM, 39048, 1), rtd="pt100"), ("850.0", "°C"))

    def test_outside_the_span(self):
        self.assertEqual(shown(pkt(OHM, 18500, 0), rtd="pt100"), ("-OFL", "°C"))
        self.assertEqual(shown(pkt(OHM, 10000, 0), rtd="pt100"), ("-OFL", "°C"))
        self.assertEqual(shown(pkt(OHM, 39050, 1), rtd="pt100"), ("OFL", "°C"))
        self.assertEqual(shown(pkt(OHM, 40000, 1), rtd="pt100"), ("OFL", "°C"))
        self.assertEqual(shown(pkt(OHM, 1000, 4), rtd="pt100"), ("OFL", "°C"))          # range 4: above every sensor

    def test_fahrenheit_pt1000_and_rel(self):
        self.assertEqual(shown(pkt(OHM, 13851, 1), rtd="pt100", unit="°F"), ("212.0", "°F"))
        self.assertEqual(shown(pkt(OHM, 13851, 2), rtd="pt1000"), ("100.0", "°C"))      # 1.3851 kΩ
        self.assertIsNotNone(view_for(pkt(OHM, 3851, 1, icon2=0x10), "off", "pt100"))  # REL: the view converts D

    def test_not_shown(self):
        self.assertIsNone(shown(pkt(OHM, 13851, 1)))
        self.assertIsNone(shown(pkt(OHM, 13851, 1, overflow=True), rtd="pt100"))
        self.assertIsNone(shown(pkt(OHM, 13851, 1, icon3=0x10), rtd="pt100"))          # logging / MEM view


class TempScaleTests(unittest.TestCase):
    def test_r_s_thermocouples_extend_the_chart(self):
        self.assertEqual(pkt(TEMP_C, 12000, 0, temp_c=True).graph_full_scale, 1350.0)   # 1200.0 °C (type K)
        self.assertEqual(pkt(TEMP_C, 17000, 0, temp_c=True).graph_full_scale, 1800.0)   # 1700.0 °C (type R / S)


if __name__ == "__main__":
    unittest.main()
