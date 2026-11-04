"""The meter's SETUP pages, named and explained, from the custom firmware's page-text frame (C-1.03+, CF-07).

The 19-byte status frame describes the secondary display only for the setup items EEVblog's v2.05 knew; for every
other page (and for v2.05's beeper, ID and logging-interval pages) it sends zeros. The custom firmware C-1.03 and later
also sends, while a SETUP page is shown, an 11-byte frame with exactly what the small display shows:

    F7 item c0 c1 c2 c3 c4 flags units 00 xor
      item    the SETUP page (setup item number; 0 = back to the normal display)
      c0..c4  the five characters of the small display (ASCII as the meter draws them)
      flags   bits 0..2 decimal point (digits after it), bit 4 minus sign, bit 5 the page is being edited
      units   bit 0 V, 1 A, 2 m, 3 k, 4 Hz, 5 dB, 6 Ω
      xor     makes the XOR of all eleven bytes zero

(firmware: ble/subtext.s in bigtimer-dev/121GW-Open-Firmware.) Older firmware never sends it; the app then shows what
the status frame says, as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FRAME_LEN = 11

# item -> (short name as on the meter, title, what it does)
PAGES: dict[int, tuple[str, str, str]] = {
    1: ("BAT", "Battery", "The battery voltage. Hold SETUP to trim it (bA): REL / PEAK in 0.2 % steps until it matches "
                          "another meter, hold SETUP to save."),
    2: ("APO", "Auto power-off", "Whether the meter switches itself off after a while without use (on / off)."),
    3: ("b", "Beeper", "Key beeps and warning tones on or off (b-On / b-OFF). Continuity always beeps."),
    4: ("Lcd", "LCD contrast", "Display contrast."),
    5: ("Yr", "Year", "The clock's year (hold SETUP to change)."),
    6: ("dAt", "Date", "The clock's month and day (hold SETUP to change)."),
    7: ("tim", "Time", "The clock's hours and minutes (hold SETUP to change; Sync Meter Clock sets it from the PC)."),
    8: ("Id", "Meter ID", "A five-digit number of your choice, written into the log files."),
    9: ("In", "Logging interval", "Seconds between log records on the SD card (0 = about 5 per second)."),
    10: ("", "Function option", "Depends on the function: continuity threshold (BEEP), burden voltage display "
                               "(current), the scaled value (V / A functions)."),
    11: ("", "Function option", "Depends on the function: dBm in ACV (hold SETUP to choose the reference 600 / 50 / 75 Ω "
                               "or dBV), burden voltage in the power functions."),
    12: ("", "Function option", "The power functions' extra value."),
    20: ("bL", "Backlight auto-off", "How long the backlight stays on: OFF = until switched off, 30S = 30 seconds, "
                                    "1..99 = minutes."),
    21: ("bt", "Bluetooth rate", "Readings sent per second over Bluetooth: 2 (default), 4, 10 or 20."),
    22: ("rEL", "REL mode", "What REL shows: d = the difference (default), P = the difference in percent."),
    23: ("tc", "Thermocouple type", "The probe type in TEMP: K (default), J, T, N, R or S."),
    24: ("dSP", "Display level", "Display averaging: 1 = steadiest (default), 2 = four times faster response."),
    25: ("CL", "Clock format", "24- or 12-hour time on the time page (logs and Bluetooth stay 24-hour)."),
    26: ("bH", "Continuity beep hold", "How long the continuity beep continues after contact is lost: 100, 50, 25 ms "
                                      "or OFF (about 15 ms)."),
    27: ("Lt", "Limit test", "Pass / fail test against the captured Lo and HI limits (hold SETUP, REL = on)."),
    28: ("Lo", "Low limit", "The limit test's low limit: hold SETUP, press REL to capture the present reading."),
    29: ("HI", "High limit", "The limit test's high limit: hold SETUP, press REL to capture the present reading."),
    30: ("tr", "Logging trigger", "When a log record is written: In = every interval, Ch = when the reading changes, "
                                 "Lt = when the limit verdict changes."),
    31: ("Ch", "Change threshold", "With trigger Ch: how many counts the reading must change before a record."),
    32: ("n", "Statistics: count", "Readings collected since MIN/MAX was switched on (k = thousands)."),
    33: ("Sd", "Statistics: deviation", "The standard deviation of the MIN/MAX readings (on the main display)."),
    34: ("tHI", "Time of the maximum", "When the MIN/MAX maximum happened (hours-minutes on the main display)."),
    35: ("tLo", "Time of the minimum", "When the MIN/MAX minimum happened (hours-minutes on the main display)."),
    36: ("", "Limit test verdict", "PASS inside the limits, HI above, Lo below."),
    37: ("LP", "4–20 mA loop view", "Shows DC mA as percent of a 4–20 mA or 0–20 mA loop (OFF = the current)."),
    38: ("Pt", "PT100 / PT1000 view", "Shows Ω as the temperature of a platinum sensor (OFF = the resistance)."),
    39: ("Ld", "Firmware over Bluetooth", "Receives a firmware file to the SD card (main menu > Upload Firmware…)."),
    100: ("MEM", "Memory view", "The logging memory page."),
}

# whole-page texts with a clearer name (the meter's own text is shown next to it)
VALUE_NAMES = {
    "APO.oN": "on", "APO.oFF": "off", "b-On": "on", "b-OFF": "off",
    "rEL-d": "difference", "rEL-P": "percent",
    "dSP 1": "1 (steadiest)", "dSP 2": "2 (faster)", "CL 24": "24-hour", "CL 12": "12-hour",
    "LPOFF": "off (shows mA)", "L4-20": "4–20 mA as %", "L0-20": "0–20 mA as %",
    "PtOFF": "off (shows Ω)", "Pt100": "PT100", "P1000": "PT1000",
    "bLOFF": "never off", "bL30S": "30 seconds", "bHOFF": "off (about 15 ms)",
    "tr-In": "every interval", "tr-Ch": "on change", "tr-Lt": "on limit verdict change",
    "Ld-bt": "waiting for a file", "Ld oK": "file received and checked",
    "tc-K": "type K", "tc-J": "type J", "tc-t": "type T", "tc-n": "type N", "tc-r": "type R", "tc-S": "type S",
}

_UNITS = ((0x04, "m"), (0x08, "k"), (0x01, "V"), (0x02, "A"), (0x40, "Ω"), (0x10, "Hz"), (0x20, "dB"))


@dataclass(frozen=True)
class SetupText:
    item: int
    chars: str          # the five characters as drawn
    decimals: int       # digits after the decimal point (0 = none)
    negative: bool
    editing: bool
    units: int

    @property
    def shown(self) -> str:
        """The small display as text, with the decimal point and the sign."""
        c = self.chars
        if self.decimals:
            k = 5 - self.decimals
            c = c[:k] + "." + c[k:]
        return ("-" if self.negative else "") + c

    @property
    def unit(self) -> str:
        return "".join(n for bit, n in _UNITS if self.units & bit)

    @property
    def name(self) -> str:
        return PAGES.get(self.item, ("", f"SETUP page {self.item}", ""))[1]

    @property
    def description(self) -> str:
        return PAGES.get(self.item, ("", "", ""))[2]

    @property
    def value(self) -> str:
        """The setting or value with the page's short name taken off, e.g. "bt 20" -> "20", "LP4-20"."""
        text = self.shown.strip()
        if text in VALUE_NAMES:
            return VALUE_NAMES[text]
        if self.item in (2, 3):                # APO.oN / APO.oF..., b-On / b-OFF
            return "on" if text.lower().endswith("n") else "off"
        short = PAGES.get(self.item, ("",))[0]
        if short and text.lower().startswith(short.lower()) and len(text) > len(short):
            rest = text[len(short):].strip()
            if rest and set(rest) <= {"-"}:
                return "not captured" if self.item in (28, 29) else "none yet"
            text = rest.lstrip(" -.")
        if self.item == 7:                     # hours-minutes; the separator blinks ('-' / ' ')
            m = re.match(r"^\s*(\d{1,2})[- ](\d\d)$", self.chars)
            if m:
                text = f"{m[1]}:{m[2]}"
        return f"{text} {self.unit}".strip() if self.unit else text


def parse_setup_frames(buf: bytes) -> tuple[list[SetupText], int]:
    """All valid F7 frames in buf, and how many leading bytes are done (keep the rest for the next call)."""
    out, i, done = [], 0, 0
    while i < len(buf):
        if buf[i] == 0xF7:
            if i + FRAME_LEN > len(buf):
                return out, i
            f = buf[i:i + FRAME_LEN]
            x = 0
            for b in f:
                x ^= b
            if x == 0 and f[9] == 0 and (f[1] <= 39 or f[1] == 100):
                fl = f[7]
                chars = "".join(chr(c) if 32 <= c < 127 else "8" if c == 0xFF else " " for c in f[2:7])
                out.append(SetupText(f[1], chars, fl & 7 if (fl & 7) <= 4 else 0, bool(fl & 0x10), bool(fl & 0x20),
                                     f[8]))
                i += FRAME_LEN
                done = i
                continue
        i += 1
        done = i
    return out, done
