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


# ---- what each key does (the open firmware's manual, "The keys"; v2.05 behaves the same for these) ------------------

_AC_MODES = {2, 4, 13, 14, 15, 16, 18, 20}                  # MeterMode values (gw121/packet.py)
_CURRENT_MODES = {16, 17, 18, 19, 20, 21}
_NO_AUTOHOLD = {6, 7, 8, 12}                                # Hz, ms, %, capacitance: HOLD has no auto-hold step


def key_help(key: MeterKey, mode: int | None = None, in_setup: bool = False) -> tuple[str, str | None]:
    """(what a press does, what holding does or None) for `key`, for the meter's present function `mode` (a MeterMode
    value, None if not known) and whether a SETUP page is shown."""
    if in_setup:
        setup = {
            MeterKey.SETUP: ("Next settings page", "Edit the page shown; hold again to save"),
            MeterKey.REL: ("▲ Step the value up (while editing)", None),
            MeterKey.PEAK: ("▼ Step the value down (while editing)",
                            "Bluetooth on / off — switching it off disconnects this app"),
        }
        if key in setup:
            return setup[key]
    if key is MeterKey.MODE:
        return "Next function of this dial position", "Backlight on / off"
    if key is MeterKey.RANGE:
        if mode == 11:
            return ("Diode test voltage 3 V ↔ 15 V (15 V only on parts that are off and discharged)",
                    "Back to auto-range")
        return "Fix the range; press again for the next range up", "Back to auto-range (AUTO)"
    if key is MeterKey.HOLD:
        if mode in _NO_AUTOHOLD:
            return "Freeze the reading → off", None
        return "Freeze the reading → auto-hold (A-HOLD: keeps each steady reading) → off", None
    if key is MeterKey.REL:
        press = "Show the difference to the present reading (REL) on / off"
        if mode in (9, 12):
            press += " — short the leads (Ω) or leave them open (capacitance) first to subtract them"
        if mode in _CURRENT_MODES:
            return press, "Store a zero offset for this current range"
        if mode in _AC_MODES:
            return press, "1 kHz low-pass filter on / off"
        return press, None
    if key is MeterKey.PEAK:
        hold = "Bluetooth on / off — switching it off disconnects this app"
        if mode in _AC_MODES:
            return "Catch short peaks on / off", hold
        return "Catch short peaks (AC functions only; two beeps here)", hold
    if key is MeterKey.MIN_MAX:
        return "Start MIN/MAX; each press shows max → min → average → all", "Stop MIN/MAX"
    if key is MeterKey.MEM:
        return "Show the last logged readings", "Start / stop logging to the SD card"
    if key is MeterKey.SETUP:
        return "Settings: next page on the small display", "Edit the page shown; hold again to save"
    return key.title, None
