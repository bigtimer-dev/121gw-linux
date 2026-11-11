"""Simulated 121GW so the app can be explored (and tested) without a meter.

It produces genuine 0xF2 frames that go through the same parser as BLE data,
and reacts to the remote keys (MODE, RANGE, HOLD, REL, MIN/MAX, MEM, SETUP).
"""

from __future__ import annotations

import math
import random
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib

from .keys import VERSION_REQUEST, MeterKey
from . import upload as _up
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
    is_demo = True                      # no SETUP page-text frames: the SETUP list in the key menu is hidden
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
        # meter clock, unset like a fresh 121GW until the app syncs it
        self._clock_offset = time.mktime((2004, 1, 1, 0, 0, 0, 0, 0, -1)) - time.time()
        self._receiver = DemoSdReceiver()

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

    # ---- commands ----------------------------------------------------------

    @property
    def card_ready(self) -> bool:
        return self.is_connected and not self.uploading      # the demo meter takes card requests at any time

    def write_frame(self, payload: bytes, done=None) -> None:
        if not self.is_connected:
            if done:
                done("Not connected")
            return
        if len(payload) == 15 and payload[0] == 0xF8:
            try:
                v = [int(payload[i:i + 2], 16) for i in range(1, 15, 2)]
            except ValueError:
                v = []
            x = 0
            for b in v[:6]:
                x ^= b
            if len(v) == 7 and x == v[6]:
                when = time.mktime((2000 + v[0], v[1], v[2], v[3], v[4], v[5], 0, 0, -1))
                self._clock_offset = when - time.time()
                GLib.timeout_add(150, self._echo, bytes(payload))     # the meter echoes the frame
        elif payload[:2] == b"\xB5\x5B":                                 # page Ld (IMP-28), see DemoSdReceiver
            answer = self._receiver.packet(bytes(payload))
            if answer:
                GLib.timeout_add(15, self._echo, answer)
        elif payload == VERSION_REQUEST:                              # as open firmware v1.0.2 (IMP-30)
            answer = bytes([0xF6]) + b"C-108"
            x = 0
            for b in answer:
                x ^= b
            GLib.timeout_add(100, self._echo, bytes(payload) + answer + bytes([x]))
        if done:
            done(None)

    def _echo(self, payload: bytes) -> bool:
        if self.is_connected:
            self.ingest(payload)
        return GLib.SOURCE_REMOVE

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
            now = time.localtime(time.time() + self._clock_offset)
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


class DemoSdReceiver:
    """The meter's side of the Bluetooth file transfer (ble/bft.s), enough for the demo: the file is kept in memory."""

    def __init__(self) -> None:
        self.size = self.crc = 0
        self.data = bytearray()
        self.expect = 0
        self.receiving = False
        self.card: bytes | None = None          # "EEVBlog.bin" after a checked transfer
        self.files = DemoCard()

    def card_reply(self, t: int, seq: int, payload: bytes) -> bytes:
        return self.files.handle(t, seq, payload)

    @staticmethod
    def _answer(seq: int, status: int, nxt: int) -> bytes:
        body = struct.pack("<BHBBH", 0x81, seq & 0xFFFF, 3, status, nxt & 0xFFFF)
        return b"\xB5\x5B" + body + struct.pack("<H", _up.crc16(body))

    def packet(self, p: bytes) -> bytes | None:
        if len(p) < 8 or len(p) != 8 + p[5]:
            return None
        body, (crc,) = p[2:-2], struct.unpack("<H", p[-2:])
        ptype, seq, n = struct.unpack_from("<BHB", body)
        payload = body[4:]
        if crc != _up.crc16(body):
            return self._answer(seq, 1, self.expect)
        if 0x10 <= ptype <= 0x17 or (self.files.fast and ptype in (0x1A, 0x1B, 0x1D, 0x1E)):
            return self.card_reply(ptype, seq, payload)
        if ptype == _up.T_START:
            self.size, self.crc = struct.unpack("<II", payload)
            if not _up.MIN_SIZE <= self.size <= _up.MAX_SIZE:
                return self._answer(seq, 4, 0)
            self.data.clear()
            self.expect, self.receiving = 0, True
            return self._answer(seq, 0, 0)
        if ptype == _up.T_ABORT:
            self.receiving = False
            return self._answer(seq, 0, 0)
        if not self.receiving:
            return self._answer(seq, 3, 0)
        if ptype == _up.T_DATA:
            if seq == self.expect - 1:
                return self._answer(seq, 0, self.expect)            # sent twice: accepted, not stored again
            if seq != self.expect:
                return self._answer(seq, 2, self.expect)
            if len(self.data) + len(payload) > self.size:
                return self._answer(seq, 5, self.expect)
            self.data += payload
            self.expect += 1
            return self._answer(seq, 0, self.expect)
        if ptype == _up.T_END:
            self.receiving = False
            if len(self.data) != self.size or zlib.crc32(self.data) & 0xFFFFFFFF != self.crc:
                return self._answer(seq, 0x12, 0)
            if _up.check_image(bytes(self.data)):
                return self._answer(seq, 0x13, 0)
            self.card = bytes(self.data)
            return self._answer(seq, 0, 0)
        return self._answer(seq, 3, 0)


class DemoCard:
    """The meter's card and settings requests (ble/bftsd.s, ble/bftset.s) over a few made-up files, for the demo."""

    fast = True                         # STREAM (C-1.07 on); False answers like C-1.06

    def __init__(self) -> None:
        import random
        rnd = random.Random(121)
        def log(n: int) -> bytes:
            rows = ["Time,Value,Unit"] + [f"12:{i // 60:02d}:{i % 60:02d},{5 + rnd.uniform(-.01, .01):.4f},VDC"
                                          for i in range(n)]
            return ("\r\n".join(rows) + "\r\n").encode()
        stamp = ((2026 - 1980) << 9 | 11 << 5 | 5, 14 << 11 | 30 << 5)
        self.files: dict[str, tuple[bytes, int, int]] = {
            "cal.bin": (bytes(512), *stamp), "EEVBlog.bin": (bytes(170000), *stamp),
            "LOG0001.CSV": (log(800), *stamp), "LOG0002.CSV": (log(2500), stamp[0], stamp[1] + 64),
            "LOG0003.CSV": (log(120), stamp[0], stamp[1] + 128),
        }
        self.staged = b""
        self.settings = {0x00F: 0, 0x010: 1, 0x011: 0, 0x013: 0, 0x014: 3, 0x015: 1, 0x016: 0, 0x017: 31,
                         0x01A: 13, 0x01B: 0, 0x01E: 1, 0x020: 0, 0x021: 0, 0x022: 10, 0x028: 0, 0x029: 0, 0x02A: 100,
                         0x02B: 0, 0x02C: 0, 0x02F: 50}
        self.wide = {0x02D: 30, 0x018: 1}  # two-byte settings (F-1.33 / C-1.10): GET flags 1, PUT id(2) value(2)

    @staticmethod
    def reply(seq: int, status: int, field: int = 0, data: bytes = b"") -> bytes:
        body = struct.pack("<BHBBH", 0x82, seq & 0xFFFF, 3 + len(data), status, field & 0xFFFF) + data
        return b"\xB5\x5B" + body + struct.pack("<H", _up.crc16(body))

    def _find(self, name: str) -> str | None:
        return next((k for k in self.files if k.lower() == name.lower()), None)

    def handle(self, t: int, seq: int, p: bytes) -> bytes:
        if t == 0x10:
            (start,) = struct.unpack_from("<H", p)
            names = list(self.files)
            out, n = b"", 0
            for name in names[start:]:
                data, d, tm = self.files[name]
                e = struct.pack("<IHHBB", len(data), d, tm, 0x20, len(name)) + name.encode()
                if len(out) + len(e) > 232:
                    break
                out += e
                n += 1
            return self.reply(seq, 0, n | (0x8000 if start + n >= len(names) else 0), out)
        if t == 0x11:
            name = p[1:1 + p[0]].decode("latin-1")
            off, ln = struct.unpack_from("<IH", p, 1 + p[0])
            k = self._find(name)
            if k is None:
                return self.reply(seq, 0x20)
            self.staged = self.files[k][0][off:off + ln]
            return self.reply(seq, 0, len(self.staged), struct.pack("<I", zlib.crc32(self.staged) & 0xFFFFFFFF))
        if t == 0x12:
            (idx,) = struct.unpack_from("<H", p)
            return self.reply(seq, 0, idx, self.staged[idx * 232:(idx + 1) * 232])
        if t in (0x1A, 0x1D):                                       # LINK (ble/bftfast.s; 0x1D since C-1.10)
            return self.reply(seq, 0, 218)
        if t in (0x1B, 0x1E):                                       # STREAM (0x1E since C-1.10)
            first, count, size = struct.unpack_from("<HHB", p)
            n = (len(self.staged) + size - 1) // size - first if size else 0
            if not 1 <= size <= 232 or n <= 0:
                return self.reply(seq, 0x22)
            count = min(count, n)
            out = self.reply(seq, 0, count)
            for i in range(first, first + count):
                d = self.staged[i * size:(i + 1) * size]
                body = struct.pack("<BHB", 0x83, i, len(d)) + d
                out += b"\xB5\x5B" + body + struct.pack("<H", _up.crc16(body))
            return out
        if t == 0x13:
            name = p[1:1 + p[0]].decode("latin-1")
            if name.lower() in ("cal.bin", "eevblog.bin", "btload.tmp"):
                return self.reply(seq, 0x23)
            k = self._find(name)
            if k is None:
                return self.reply(seq, 0x20)
            del self.files[k]
            return self.reply(seq, 0)
        if t == 0x15:                                               # FORMAT (F-1.09 / C-1.08 on)
            if p != b"FORMAT":
                return self.reply(seq, 0x22)
            self.files.clear()
            self.staged = b""
            return self.reply(seq, 0)
        if t == 0x14:
            used = sum((len(v[0]) + 32767) // 32768 for v in self.files.values())
            return self.reply(seq, 0, 64, struct.pack("<II", 242000 - used, 242304))
        if t == 0x16:
            if len(p) == 3 and p[2] == 1:                           # IMP-64: every setting, two-byte values, pages
                items = list(self.settings.items()) + list(self.wide.items())
                part = items[p[0]:p[0] + p[1]]
                end = 0x8000 if p[0] + len(part) >= len(items) else 0
                return self.reply(seq, 0, len(part) | end, b"".join(struct.pack("<HH", k, v) for k, v in part))
            if p:
                return self.reply(seq, 0x22)
            return self.reply(seq, 0, len(self.settings), b"".join(struct.pack("<HB", k, v)
                                                                     for k, v in self.settings.items()))
        if t == 0x17:
            if len(p) == 4:
                wid, v = struct.unpack_from("<HH", p)
                if wid not in self.wide:
                    return self.reply(seq, 0x22)
                self.wide[wid] = v
                return self.reply(seq, 0, wid, struct.pack("<H", v))
            wid, v = struct.unpack_from("<HB", p)
            if wid not in self.settings:
                return self.reply(seq, 0x22)
            self.settings[wid] = v
            return self.reply(seq, 0, wid, bytes([v]))
        return self.reply(seq, 3)
