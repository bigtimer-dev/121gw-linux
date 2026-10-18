"""Simulated 121GW so the app can be explored (and tested) without a meter.

It produces genuine 0xF2 frames that go through the same parser as BLE data,
and reacts to the remote keys (MODE, RANGE, HOLD, REL, MIN/MAX, MEM, SETUP).
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib

from .keys import MeterKey
from .link import DiscoveredDevice, MeterLink
from .packet import RANGES, MeterMode, build_frame

M = MeterMode
DEMO_ADDRESS = "00:00:00:12:1G:W0"
FRAME_MS = 250

_MULT = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6}


@dataclass
class DemoFunction:
    mode: MeterMode
    signal: Callable[[float], float]      # value in the mode's base unit (V, Ω, µA for µA modes…)
    acdc: int = 0                         # 1 DC, 2 AC, 3 DC+AC
    sub: str = "temp"                     # "temp", "hz", "beep", "none"


def _noise(scale: float) -> float:
    return random.gauss(0, scale)


FUNCTIONS = [
    DemoFunction(M.DCV, lambda t: 3.3 + 0.08 * math.sin(t / 3) + _noise(0.002), acdc=1),
    DemoFunction(M.ACV, lambda t: 230.0 + 2.5 * math.sin(t / 7) + _noise(0.2), acdc=2, sub="hz"),
    DemoFunction(M.DC_MV, lambda t: 0.0125 + 0.004 * math.sin(t) + _noise(0.0001), acdc=1),
    DemoFunction(M.RESISTOR, lambda t: 4700 * (1 + 0.002 * math.sin(t / 5)) + _noise(0.5)),
    DemoFunction(M.CONTINUITY, lambda t: 0.4 + abs(_noise(0.05)) if (t % 6) < 2.5 else math.inf, sub="beep"),
    DemoFunction(M.DIODE, lambda t: 0.62 + 0.004 * math.sin(t) + _noise(0.0005) if (t % 8) < 5 else math.inf),
    DemoFunction(M.DC_MA, lambda t: 0.0185 + 0.006 * math.sin(t / 2) + _noise(0.00005), acdc=1),
    DemoFunction(M.HZ, lambda t: 1000.0 + 0.5 * math.sin(t / 4) + _noise(0.05)),
    DemoFunction(M.CAPACITOR, lambda t: 10.1e-6 + _noise(0.01e-6)),
    DemoFunction(M.TEMP_C, lambda t: 24.0 + 1.5 * math.sin(t / 20) + _noise(0.05)),
]

SETUP_PAGES = [110, 140, 130, 135, 101, 120, 160, 190]


def encode(mode: MeterMode, value: float, range_index: int | None) -> tuple[int, int, bool, bool]:
    """Return (range_index, int_value, negative, overflow) for a base-unit value."""
    points, notation = RANGES[int(mode)]
    candidates = [range_index] if range_index is not None else range(len(points))
    if not math.isfinite(value):
        return (range_index or len(points) - 1), 0, False, True
    for i in candidates:
        disp = abs(value) / _MULT.get(notation[i], 1.0)
        n = round(disp * 10 ** (5 - points[i]))
        if n <= 50_999:
            return i, n, value < 0, False
    return candidates[-1], 0, False, True


class DemoLink(MeterLink):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._timer = 0
        self._t0 = time.monotonic()
        self.fn_index = 0
        self.manual_range: int | None = None
        self.hold = 0                      # 0 off, 2 HOLD, 1 Auto-HOLD (meter cycle order)
        self.hold_frame: bytes | None = None
        self.rel_offset: float | None = None
        self.min_max = 0
        self.mem = False
        self.setup_page: int | None = None
        self._last_value = 0.0
        self._minmax_vals: list[float] = []

    def start(self) -> None:
        self.devices[DEMO_ADDRESS] = DiscoveredDevice(DEMO_ADDRESS, "121GW (demo)", rssi=-58)
        self.rssi = -58
        if self.settings.auto_reconnect:
            self.connect_device(DEMO_ADDRESS)
        else:
            self.set_status("Demo meter ready")
        self.changed()

    def start_scan(self) -> None:
        self.is_scanning = True
        self.set_status("Scanning for 121GW…")
        GLib.timeout_add(800, self._scan_done)

    def _scan_done(self) -> bool:
        self.is_scanning = False
        self.set_status("Scan finished")
        return GLib.SOURCE_REMOVE

    def stop_scan(self) -> None:
        self.is_scanning = False
        self.changed()

    def connect_device(self, address: str) -> None:
        if self.is_connected:
            return
        self.connected_address = address
        self.connected_name = "121GW (demo)"
        self.devices[address].connected = True
        self.is_connected = True
        self.communication_lost = False
        self.set_status("Live (demo)")
        self._start_watchdog()
        self._timer = GLib.timeout_add(FRAME_MS, self._tick)
        self.changed()

    def disconnect(self) -> None:
        self.set_auto_reconnect(False)
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self.connected_address in self.devices:
            self.devices[self.connected_address].connected = False
        self._reset_link_state()
        self.connected_address = self.connected_name = None
        self.latest = None
        self.set_status("Disconnected")
        self.changed()

    def set_auto_reconnect(self, value: bool) -> None:
        self.settings.auto_reconnect = value
        self.changed()

    def shutdown(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
        self._stop_watchdog()

    # ---- keys --------------------------------------------------------------

    def send_key(self, key: MeterKey, long_press: bool = False) -> None:
        if not self.is_connected:
            self.set_status("Not connected")
            return
        self.note_key(key)
        if self.setup_page is not None and key in (MeterKey.REL, MeterKey.PEAK):
            step = -1 if key is MeterKey.REL else 1
            i = SETUP_PAGES.index(self.setup_page)
            self.setup_page = SETUP_PAGES[(i + step) % len(SETUP_PAGES)]
        elif key is MeterKey.SETUP:
            self.setup_page = None if self.setup_page is not None else SETUP_PAGES[0]
        elif key is MeterKey.MODE and not long_press:
            self.setup_page = None
            self.fn_index = (self.fn_index + 1) % len(FUNCTIONS)
            self.manual_range = None
            self.rel_offset = None
            self.min_max = 0
            self.hold = 0
        elif key is MeterKey.RANGE:
            if long_press:
                self.manual_range = None
            else:
                n = len(RANGES[int(FUNCTIONS[self.fn_index].mode)][0])
                cur = self.manual_range if self.manual_range is not None else self._auto_range()
                self.manual_range = (cur + 1) % n
        elif key is MeterKey.HOLD:
            self.hold = {0: 2, 2: 1, 1: 0}[self.hold]
            self.hold_frame = None
        elif key is MeterKey.REL:
            self.rel_offset = None if self.rel_offset is not None else self._last_value
        elif key is MeterKey.MIN_MAX:
            self.min_max = 0 if long_press else (self.min_max % 4) + 1
            if self.min_max == 1:
                self._minmax_vals = []
        elif key is MeterKey.MEM:
            self.mem = not self.mem
        self.set_status(self.key_status(key, long_press))
        self._tick()

    def _auto_range(self) -> int:
        fn = FUNCTIONS[self.fn_index]
        return encode(fn.mode, self._last_value, None)[0]

    # ---- frame generation --------------------------------------------------

    def _tick(self) -> bool:
        if not self.is_connected:
            return GLib.SOURCE_REMOVE
        if self.hold and self.hold_frame is not None:
            self.ingest(self.hold_frame)
            return GLib.SOURCE_CONTINUE
        frame = self._frame(time.monotonic() - self._t0)
        if self.hold:
            self.hold_frame = frame
        self.ingest(frame)
        return GLib.SOURCE_CONTINUE

    def _frame(self, t: float) -> bytes:
        fn = FUNCTIONS[self.fn_index]
        v = fn.signal(t)
        if math.isfinite(v):
            self._last_value = v
            if self.min_max:
                self._minmax_vals.append(v)
                vals = self._minmax_vals
                v = {1: max(vals), 2: min(vals), 3: sum(vals) / len(vals), 4: v}[self.min_max]
            if self.rel_offset is not None:
                v -= self.rel_offset
        rng, n, neg, ovf = encode(fn.mode, v, self.manual_range)

        icon1 = (fn.acdc << 3) | (0x04 if self.manual_range is None else 0)
        icon2 = 0x40 | (0x10 if self.rel_offset is not None else 0) | self.min_max
        icon3 = (self.hold << 2) | (0x10 if self.mem else 0)
        if fn.acdc == 1:
            icon3 |= 0x01
        elif fn.acdc == 2:
            icon3 |= 0x02

        temp = 24.0 + 0.6 * math.sin(t / 30) + _noise(0.03)
        sub_mode, sub_val, sub_pt = 0, 0, 0
        if self.setup_page is not None:
            sub_mode = self.setup_page
            now = time.localtime()
            sub_val, sub_pt = {
                110: (round(5.08 * 100), 2),
                140: ((now.tm_hour << 8) | now.tm_min, 0),
                130: (now.tm_year, 0),
                135: ((now.tm_mon << 8) | now.tm_mday, 0),
                101: (1, 0),
                120: (0, 0),
                160: (5, 0),
                190: (12345, 0),
            }[sub_mode]
        elif fn.sub == "temp":
            sub_mode, sub_val, sub_pt = 5, round(temp * 10), 1
        elif fn.sub == "hz":
            sub_mode, sub_val, sub_pt = 0, round((50.0 + _noise(0.005)) * 100), 2
        elif fn.sub == "beep":
            sub_mode, sub_val = 1, 30

        points, notation = RANGES[int(fn.mode)]
        bar = 0
        if not ovf:
            full = 5 * 10 ** (points[rng] - 1)  # displayed full-scale value of this range
            disp = abs(v) / _MULT.get(notation[rng], 1.0)
            bar = min(25, int(disp / full * 26))
        bar_flags = (0x04 if neg else 0) | min(3, rng)

        return build_frame(
            int(fn.mode), n, rng, negative=neg, overflow=ovf,
            temp_c=fn.mode == M.TEMP_C,
            sub_mode=sub_mode, sub_value=sub_val, sub_point=sub_pt,
            bar_flags=bar_flags, bar_value=bar,
            icon1=icon1, icon2=icon2, icon3=icon3,
        )
