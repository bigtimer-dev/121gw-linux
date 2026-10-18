"""Remote key codes understood by the 121GW (write to the data characteristic)."""

from __future__ import annotations

from enum import Enum


class MeterKey(Enum):
    HOLD = ("HOLD", 0x32, None, "h")
    MODE = ("MODE", 0x35, None, "m")
    REL = ("REL", 0x33, "▲", "r")
    RANGE = ("RANGE", 0x31, None, "g")
    MIN_MAX = ("MIN/MAX", 0x36, None, "x")
    MEM = ("MEM", 0x37, None, "e")
    PEAK = ("PEAK", 0x34, "▼", "p")
    SETUP = ("SETUP", 0x38, None, "s")

    def __init__(self, title: str, code: int, setup_arrow: str | None, shortcut: str):
        self.title = title
        self.code = code
        self.setup_arrow = setup_arrow
        self.shortcut = shortcut

    @property
    def long_press_ms(self) -> int:
        return 1000 if self is MeterKey.MODE else 600

    def payload(self, long_press: bool = False) -> bytes:
        kind = 0x38 if long_press else 0x30
        return bytes([0xF4, kind, self.code, kind, self.code])


# Layout of the on-screen key pad, matching the meter / iOS app.
KEYPAD = [
    MeterKey.HOLD, MeterKey.MODE, MeterKey.REL, MeterKey.RANGE,
    MeterKey.MIN_MAX, MeterKey.MEM, MeterKey.PEAK, MeterKey.SETUP,
]
