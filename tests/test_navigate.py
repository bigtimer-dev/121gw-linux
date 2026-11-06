"""Key navigation (gw121/navigate.py) against a fake meter that reacts to key presses like the real one."""
import time
import unittest

from gi.repository import GLib

from gw121 import navigate as N
from gw121.keys import MeterKey
from gw121.packet import Coupling, MeterMode as M
from gw121.setup_pages import SetupText


def spin(seconds):
    end = time.monotonic() + seconds
    ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


class Pkt:
    def __init__(self, meter):
        self.mode = M(meter.mode)
        self.coupling = Coupling.ACDC if meter.acdc else (Coupling.AC if meter.mode == M.ACV else Coupling.DC)
        self.icon_auto = meter.auto
        self.main_range_index = meter.range
        self.icon3 = meter.hold << 2
        self.icon2 = meter.minmax
        self.icon_min_max = bool(meter.minmax)


class FakeMeter:
    """Reacts to keys after `delay` s and reports in frames every `period` s (the order of cycle.s / setup.s)."""

    def __init__(self, mode=M.DCV, delay=0.05, period=0.05, deaf=False):
        self.mode, self.acdc, self.auto, self.range, self.hold, self.minmax = int(mode), False, True, 1, 0, 0
        self.item = 0
        self.pages = N.setup_pages(int(mode))
        self.delay, self.period, self.deaf = delay, period, deaf
        self.is_connected = True
        self.firmware_version = "C-1.07"            # steps with the SETUP key (GotoMeter: C-1.08 jumps)
        self.bootloader = "B-1.12"
        self.on_ld_page = False
        self.setup_text = None
        self.latest = Pkt(self)
        self._last_packet_at = time.monotonic()
        self._setup_at = 0.0
        self.presses = []
        GLib.timeout_add(int(period * 1000), self._frame)

    def _frame(self):
        self.latest = Pkt(self)
        self._last_packet_at = time.monotonic()
        return self.is_connected

    def send_key(self, key, long_press=False):
        self.presses.append((key, long_press))
        if not self.deaf:
            GLib.timeout_add(int(self.delay * 1000), lambda: (self._apply(key, long_press), False)[1])

    def _apply(self, key, long_press):
        if self.on_ld_page:
            return                                  # page Ld: the link belongs to the file transfer
        if key is MeterKey.MODE:
            for g in N.MODE_GROUPS:
                states = [(int(m), v) for m, v in g]
                now = (self.mode, ("ACDC" if self.acdc else "AC") if self.mode == M.ACV else None)
                if now in states:
                    self.mode, v = states[(states.index(now) + 1) % len(states)]
                    self.acdc = v == "ACDC"
                    break
        elif key is MeterKey.RANGE:
            if long_press:
                self.auto = True
            elif self.auto:
                self.auto = False
            else:
                self.range = (self.range + 1) % 4
        elif key is MeterKey.HOLD:
            self.hold = {0: 2, 2: 1, 1: 0}[self.hold]
        elif key is MeterKey.MIN_MAX:
            self.minmax = 0 if long_press else self.minmax % 4 + 1
        elif key is MeterKey.SETUP:
            order = [0] + self.pages
            self.item = order[(order.index(self.item) + 1) % len(order)]
            self.on_ld_page = self.item == 39
            if self.item != 39:                     # no frame on Ld
                self.setup_text = None if self.item == 0 else SetupText(self.item, "     ", 0, False, False, 0)
                self._setup_at = time.monotonic()


def go(meter, key, label, wait=20):
    opts = N.options_for(key, meter)
    opt = next(o for o in opts if o.label.startswith(label))
    result = []
    nav = N.Navigator(meter, key, opt, lambda ok, t: result.append((ok, t)),
                      limit=1 if opt.long_press else N.max_presses(key, opts), step_wait_s=0.6, poll_ms=20)
    nav.start()
    end = time.monotonic() + wait
    while not result and time.monotonic() < end:
        spin(0.02)
    return result[0] if result else None


class NavigateTests(unittest.TestCase):
    def test_mode_ac_dc(self):
        m = FakeMeter(M.DCV)
        self.assertEqual([o.label for o in N.options_for(MeterKey.MODE, m)], ["DC V", "AC V", "AC+DC V"])
        ok, _ = go(m, MeterKey.MODE, "AC+DC")
        self.assertTrue(ok)
        self.assertEqual((m.mode, m.acdc), (M.ACV, True))
        self.assertEqual(len(m.presses), 2)

    def test_mode_resistance_group(self):
        m = FakeMeter(M.RESISTOR)
        self.assertEqual([o.label for o in N.options_for(MeterKey.MODE, m)],
                         ["Resistance", "Continuity", "Diode", "Capacitance"])
        self.assertTrue(go(m, MeterKey.MODE, "Capacitance")[0])
        self.assertEqual(m.mode, M.CAPACITOR)

    def test_range_fixed_and_auto(self):
        m = FakeMeter(M.DCV)
        labels = [o.label for o in N.options_for(MeterKey.RANGE, m)]
        self.assertEqual(labels, ["Auto", "5 V", "50 V", "500 V", "600 V"])
        self.assertTrue(go(m, MeterKey.RANGE, "500 V")[0])
        self.assertEqual((m.auto, m.range), (False, 2))
        self.assertTrue(go(m, MeterKey.RANGE, "Auto")[0])
        self.assertTrue(m.auto)
        self.assertEqual(m.presses[-1], (MeterKey.RANGE, True))

    def test_hold_and_minmax(self):
        m = FakeMeter(M.DCV)
        self.assertTrue(go(m, MeterKey.HOLD, "Auto-hold")[0])
        self.assertEqual(m.hold, 1)
        self.assertTrue(go(m, MeterKey.MIN_MAX, "AVG")[0])
        self.assertEqual(m.minmax, 3)
        self.assertTrue(go(m, MeterKey.MIN_MAX, "Off")[0])
        self.assertEqual(m.minmax, 0)

    def test_setup_page_before_ld(self):
        m = FakeMeter(M.DCV)
        labels = [o.label for o in N.options_for(MeterKey.SETUP, m)]
        self.assertEqual(labels[0], "Normal display")
        self.assertTrue(go(m, MeterKey.SETUP, "In")[0])
        self.assertEqual(m.item, 9)
        back = N.options_for(MeterKey.SETUP, m)[0]
        self.assertTrue(back.blocked)               # the way back to the normal display passes Ld

    def test_pages_past_ld_are_blocked_and_ld_is_reachable(self):
        m = FakeMeter(M.DCV)
        opts = {o.label.split(" ·")[0]: o for o in N.options_for(MeterKey.SETUP, m)}
        self.assertTrue(opts["rEL"].blocked)
        self.assertTrue(opts["tr"].blocked)
        self.assertFalse(opts["bt"].blocked)
        self.assertFalse(opts["Ld"].blocked)
        self.assertTrue(go(m, MeterKey.SETUP, "Ld")[0])
        self.assertTrue(m.on_ld_page)

    def test_after_ld_the_later_pages_and_the_way_back_work(self):
        m = FakeMeter(M.DCV)
        m.item = 22                                 # rEL, after the user pressed SETUP past Ld on the meter
        m.setup_text = SetupText(22, "rEL-d", 0, False, False, 0)
        opts = {o.label.split(" ·")[0]: o for o in N.options_for(MeterKey.SETUP, m)}
        self.assertFalse(opts["tr"].blocked)
        self.assertFalse(opts["BAT"].blocked)       # past tr: 0, then 1 (no Ld on the way)
        self.assertTrue(go(m, MeterKey.SETUP, "tr")[0])
        self.assertEqual(m.item, 30)
        self.assertTrue(go(m, MeterKey.SETUP, "Normal display")[0])
        self.assertEqual(m.item, 0)

    def test_setup_only_on_custom_firmware(self):
        m = FakeMeter(M.DCV)
        m.firmware_version = "F-1.12"
        self.assertEqual(N.options_for(MeterKey.SETUP, m), [])

    def test_stops_when_the_meter_does_not_react(self):
        m = FakeMeter(M.DCV, deaf=True)
        ok, text = go(m, MeterKey.MODE, "AC V")
        self.assertFalse(ok)
        self.assertIn("does not react", text)
        self.assertEqual(len(m.presses), 2)

    def test_mode_pages_follow_the_function(self):
        self.assertIn(23, N.setup_pages(int(M.TEMP)))
        self.assertNotIn(23, N.setup_pages(int(M.DCV)))
        self.assertIn(26, N.setup_pages(int(M.CONTINUITY)))
        self.assertIn(38, N.setup_pages(int(M.RESISTOR)))
        self.assertIn(37, N.setup_pages(int(M.DC_MA)))
        self.assertIn(40, N.setup_pages(int(M.DCV), has_bootloader=True))


if __name__ == "__main__":
    unittest.main()


class GotoTests(unittest.TestCase):
    """C-1.08+ (CF-09): the meter jumps to the page itself."""

    def jump(self, m, label, on_ld_request=None):
        opts = N.options_for(MeterKey.SETUP, m)
        opt = next(o for o in opts if o.label.startswith(label))
        self.assertFalse(opt.blocked, label)
        sent, result = [], []

        def direct(item=opt.item):
            sent.append(item)
            if m.on_ld_page:
                (on_ld_request or (lambda i: None))(item)
                return
            GLib.timeout_add(50, lambda: (m.jump(item), False)[1])

        nav = N.Navigator(m, MeterKey.SETUP, opt, lambda ok, t: result.append((ok, t)), step_wait_s=0.6, poll_ms=20)
        nav.start(direct)
        end = time.monotonic() + 5
        while not result and time.monotonic() < end:
            spin(0.02)
        return result[0], sent

    def test_frame(self):
        self.assertEqual(N.goto_frame(0), b"\xF4C0C0")
        self.assertEqual(N.goto_frame(22), b"\xF4D6D6")
        self.assertEqual(N.goto_frame(40), b"\xF4E8E8")

    def test_no_page_is_blocked_and_one_request_is_enough(self):
        m = GotoMeter(M.DCV)
        self.assertTrue(N.supports_goto(m))
        self.assertFalse(any(o.blocked for o in N.options_for(MeterKey.SETUP, m)))
        (ok, _), sent = self.jump(m, "tr")
        self.assertTrue(ok)
        self.assertEqual((m.item, sent, m.presses), (30, [30], []))
        m.item = 9
        m.setup_text = SetupText(9, "In  1", 0, False, False, 0)
        (ok, _), sent = self.jump(m, "Normal display")
        self.assertTrue(ok)
        self.assertEqual(m.item, 0)

    def test_from_ld_through_the_card_request(self):
        m = GotoMeter(M.DCV)
        m.item, m.on_ld_page, m.setup_text = 39, True, None
        opts = N.options_for(MeterKey.SETUP, m)
        self.assertTrue(next(o for o in opts if o.label.startswith("Ld")).current)
        asked = []
        (ok, _), sent = self.jump(m, "dSP", on_ld_request=lambda i: (asked.append(i), m.jump(i)))
        self.assertTrue(ok)
        self.assertEqual((asked, m.item, m.on_ld_page), ([24], 24, False))

    def test_older_custom_firmware_steps(self):
        m = GotoMeter(M.DCV)
        m.firmware_version = "C-1.07"
        self.assertFalse(N.supports_goto(m))
        self.assertTrue(any(o.blocked for o in N.options_for(MeterKey.SETUP, m)))


class GotoMeter(FakeMeter):
    def __init__(self, mode=M.DCV):
        super().__init__(mode)
        self.firmware_version = "C-1.08"

    def jump(self, item):
        self.item = item
        self.on_ld_page = item == 39
        if item != 39:
            self.setup_text = None if item == 0 else SetupText(item, "     ", 0, False, False, 0)
            self._setup_at = time.monotonic()
