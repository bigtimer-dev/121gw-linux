import math
import unittest

from gw121.keys import MeterKey
from gw121.packet import Coupling, FrameAssembler, MeterMode, PacketV2, build_frame
from gw121.sessions import SessionStore


def pkt(*a, **kw) -> PacketV2:
    p = PacketV2.try_parse(build_frame(*a, **kw))
    assert p is not None
    return p


class PacketTests(unittest.TestCase):
    def test_checksum_rejects_corruption(self):
        f = bytearray(build_frame(1, 12345, 1))
        self.assertIsNotNone(PacketV2.try_parse(f))
        f[8] ^= 1
        self.assertIsNone(PacketV2.try_parse(f))

    def test_dcv_value_and_format(self):
        p = pkt(1, 12345, 1, icon1=1 << 3)  # 49.999 V range, DC
        self.assertEqual(p.mode, MeterMode.DCV)
        self.assertAlmostEqual(p.main_value, 12.345)
        self.assertEqual(p.formatted_main_value, "12.345")
        self.assertEqual(p.main_unit, "V")
        self.assertEqual(p.coupling, Coupling.DC)
        self.assertEqual(p.graph_full_scale, 50)
        self.assertEqual(p.graph_lower_bound, -50)

    def test_leading_zero_blanking_and_negative(self):
        p = pkt(1, 330, 1, negative=True)
        self.assertEqual(p.formatted_main_value, " 0.330")
        self.assertAlmostEqual(p.main_value, -0.33)
        self.assertEqual(p.display_string, "-0.330 V")

    def test_18bit_value(self):
        p = pkt(9, 0x1_2345, 0)
        self.assertEqual(p.main_int_value, 0x1_2345)

    def test_overflow(self):
        p = pkt(9, 0, 6, overflow=True)
        self.assertTrue(math.isnan(p.main_value))
        self.assertEqual(p.formatted_main_value, "OL")
        self.assertEqual(p.display_string, "OL MΩ")

    def test_resistance_kilo(self):
        p = pkt(9, 47000, 3)  # 49.999 kΩ range
        self.assertEqual(p.main_unit, "kΩ")
        self.assertAlmostEqual(p.si_value, 47000.0)
        self.assertAlmostEqual(p.graph_value, 47000.0)

    def test_millivolt_and_milliamp_graph_units(self):
        p = pkt(3, 12500, 0)  # DC mV, 2 points, notation m
        self.assertEqual(p.main_unit, "mV")
        self.assertEqual(p.formatted_main_value, "12.500")
        self.assertAlmostEqual(p.graph_value, 12.5)
        q = pkt(19, 18500, 0)
        self.assertEqual(q.main_unit, "mA")
        self.assertAlmostEqual(q.graph_value, 1.85)

    def test_capacitor(self):
        p = pkt(12, 10100, 2)
        self.assertEqual(p.main_unit, "µF")
        self.assertAlmostEqual(p.graph_value, 10.1)

    def test_temperature_sub_display(self):
        p = pkt(1, 1000, 0, sub_mode=5, sub_value=235, sub_point=1)
        self.assertTrue(p.sub_is_temperature)
        self.assertEqual(p.formatted_sub_value, "23.5 °C")

    def test_acv_dual_frequency(self):
        p = pkt(2, 23000, 2, sub_mode=0, sub_value=5000, sub_point=2, icon1=2 << 3)
        self.assertTrue(p.dual_display_is_frequency)
        self.assertEqual(p.formatted_sub_value, "50.00 Hz")
        self.assertEqual(p.coupling, Coupling.AC)

    def test_setup_pages(self):
        self.assertEqual(pkt(1, 0, 0, sub_mode=110, sub_value=508, sub_point=2).setup_descriptor().reading, "5.08 V")
        self.assertEqual(pkt(1, 0, 0, sub_mode=140, sub_value=(13 << 8) | 7).setup_descriptor().reading, "13:07")
        self.assertEqual(pkt(1, 0, 0, sub_mode=135, sub_value=(10 << 8) | 18).setup_descriptor().reading, "10-18")
        self.assertEqual(pkt(1, 0, 0, sub_mode=130, sub_value=2026).setup_descriptor().reading, "2026")

    def test_continuity_beeper(self):
        p = pkt(10, 30, 0, sub_mode=1, sub_value=30)
        self.assertEqual(p.continuity_beeper_ohms, 30.0)
        self.assertEqual(p.formatted_sub_value, "30 Ω")

    def test_icons(self):
        p = pkt(1, 0, 0, icon1=0x04 | 0x01, icon2=0x40 | 0x10 | 3, icon3=0x04 | 0x10)
        self.assertTrue(p.icon_auto and p.icon_bat and p.icon_bt and p.icon_rel)
        self.assertTrue(p.icon_hold and p.icon_mem and p.icon_min_max)
        self.assertEqual(p.min_max_label, "AVG")

    def test_hold_cycle_from_real_meter(self):
        # frames captured from a real 121GW in Continuity, open circuit
        frames = {"off": "f2178000000a8000006401011a0237024000e6",
                  "hold": "f2178000000a8000006401011a0219024008c0",
                  "auto": "f2178000000a8000006401011a0219024004cc"}
        p = {k: PacketV2.try_parse(bytes.fromhex(v)) for k, v in frames.items()}
        self.assertTrue(all(p.values()))
        self.assertFalse(p["off"].icon_hold)
        self.assertTrue(p["hold"].icon_hold and not p["hold"].icon_auto_hold)
        self.assertTrue(p["auto"].icon_hold and p["auto"].icon_auto_hold)
        self.assertEqual(p["off"].mode, MeterMode.CONTINUITY)
        self.assertEqual(p["off"].display_string, "OL Ω")
        self.assertEqual(p["off"].formatted_sub_value, "28.2 °C")
        self.assertTrue(p["off"].icon_bt and p["off"].icon_apo)

    def test_assembler_resyncs_across_garbage_and_splits(self):
        a = FrameAssembler()
        f1, f2 = build_frame(1, 100, 1), build_frame(1, 200, 1)
        stream = b"\x00\xf2\x11" + f1 + f2
        out = a.feed(stream[:10]) + a.feed(stream[10:25]) + a.feed(stream[25:])
        self.assertEqual([p.main_int_value for p in out], [100, 200])

    def test_key_payload(self):
        self.assertEqual(MeterKey.MODE.payload(), bytes([0xF4, 0x30, 0x35, 0x30, 0x35]))
        self.assertEqual(MeterKey.MODE.payload(True), bytes([0xF4, 0x38, 0x35, 0x38, 0x35]))


class SessionTests(unittest.TestCase):
    def test_logging_pauses_on_function_change(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            s = SessionStore(pathlib.Path(d) / "s.json")
            s.start_logging()
            self.assertTrue(s.append(pkt(1, 100, 1, icon1=1 << 3)))
            self.assertTrue(s.append(pkt(1, 100, 2, icon1=1 << 3)))  # range change is fine
            self.assertFalse(s.append(pkt(9, 100, 1)))             # function change stops
            self.assertFalse(s.is_logging)
            saved = s.save_current("x")
            self.assertEqual(len(saved.points), 2)
            again = SessionStore(pathlib.Path(d) / "s.json")
            self.assertEqual(again.sessions[0].name, "x")
            self.assertIn("timestamp,value", again.sessions[0].csv_string())


if __name__ == "__main__":
    unittest.main()
