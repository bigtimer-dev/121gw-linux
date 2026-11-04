"""Remote key codes understood by the 121GW (write to the data characteristic)."""

from __future__ import annotations

from datetime import datetime
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


# Firmware version request of zombu4's open firmware v1.0.1+ (IMP-30): a key frame with the unused code 0x90, echoed like
# any key frame and answered with F6 + the five characters of the power-on version + XOR. v2.05 ignores the code.
VERSION_REQUEST = bytes([0xF4, 0x39, 0x30, 0x39, 0x30])


def parse_version_answer(buf: bytes) -> str | None:
    """The version ("F-1.02", "C-1.02") from the first valid F6 answer in buf, else None."""
    for i in range(len(buf) - 6):
        f = buf[i:i + 7]
        if f[0] != 0xF6:
            continue
        x = 0
        for b in f:
            x ^= b
        c = f[1:6]
        if x == 0 and chr(c[1]) == "-" and chr(c[0]).isalpha() and all(chr(d).isdigit() for d in c[2:]):
            return f"{chr(c[0])}-{chr(c[2])}.{chr(c[3])}{chr(c[4])}"
    return None


def clock_applies_every_time(version: str | None) -> bool:
    """Open firmware v1.0.1 and later (IMP-31) apply every set-clock frame; v2.05 only the first per Bluetooth-on."""
    if not version or version[0] not in "FC":
        return False
    try:
        return float(version[2:]) >= 1.01
    except ValueError:
        return False


# Layout of the on-screen key pad, matching the meter / iOS app.
KEYPAD = [
    MeterKey.HOLD, MeterKey.MODE, MeterKey.REL, MeterKey.RANGE,
    MeterKey.MIN_MAX, MeterKey.MEM, MeterKey.PEAK, MeterKey.SETUP,
]


def clock_payload(when: datetime) -> bytes:
    """Set-clock frame (15 bytes): 0xF8, then year - 2000, month, day, hour, minute, second and the XOR of those six,
    each as two upper-case ASCII hex digits.

    The meter echoes a valid frame. It applies the first one after power-on or after its Bluetooth is switched on
    (PEAK long press) and ignores further ones until Bluetooth is switched off and on again. Same frame and rule on
    the original v2.05 firmware and on zombu4's open firmware.
    """
    if not 2000 <= when.year <= 2099:
        raise ValueError(f"the meter's clock holds the years 2000-2099, not {when.year}")
    fields = [when.year - 2000, when.month, when.day, when.hour, when.minute, when.second]
    check = 0
    for f in fields:
        check ^= f
    return b"\xF8" + "".join(f"{f:02X}" for f in fields + [check]).encode("ascii")
