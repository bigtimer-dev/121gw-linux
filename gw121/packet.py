"""121GW BLE packet format v2 decoder.

Faithful port of PacketV2 from the iOS / Windows / Android companion apps:
19-byte frames starting with 0xF2, XOR checksum in the last byte.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum, IntEnum

LENGTH = 19
START_BYTE = 0xF2


class MeterMode(IntEnum):
    LOW_Z = 0
    DCV = 1
    ACV = 2
    DC_MV = 3
    AC_MV = 4
    TEMP = 5
    HZ = 6
    MS = 7
    DUTY = 8
    RESISTOR = 9
    CONTINUITY = 10
    DIODE = 11
    CAPACITOR = 12
    AC_UVA = 13
    AC_MVA = 14
    AC_VA = 15
    AC_UA = 16
    DC_UA = 17
    AC_MA = 18
    DC_MA = 19
    AC_A = 20
    DC_A = 21
    DC_UVA = 22
    DC_MVA = 23
    DC_VA = 24
    TEMP_C = 25
    TEMP_F = 26
    UNKNOWN = 255


M = MeterMode
_DEFINED_MODES = {m.value for m in MeterMode}


class Coupling(Enum):
    NONE = "none"
    DC = "DC"
    AC = "AC"
    ACDC = "DC+AC"


class MeasurementPosition(Enum):
    VOLTAGE = 0
    MILLIVOLT_TEMPERATURE = 1
    RESISTANCE = 2
    FREQUENCY = 3
    CURRENT = 4
    MICRO_CURRENT = 5
    POWER_CURRENT = 6
    UNKNOWN = 7


class SetupItem(Enum):
    TEMPERATURE = 0
    BATTERY = 1
    AUTO_POWER_OFF = 2
    BUZZER = 3
    LCD_CONTRAST = 4
    YEAR = 5
    DATE = 6
    TIME = 7
    METER_ID = 8
    LOGGING_INTERVAL = 9
    CONTINUITY_THRESHOLD = 10
    BURDEN_VOLTAGE = 11
    DIODE_RANGE = 12
    FREQUENCY_MODE = 13
    BACKLIGHT = 14
    UNKNOWN = 15


@dataclass(frozen=True)
class SetupDescriptor:
    item: SetupItem
    title: str
    reading: str


@dataclass(frozen=True)
class SubPresentation:
    digits: str
    unit: str
    label: str


# mode -> (decimal-point positions per range, SI notation per range)
RANGES: dict[int, tuple[list[int], str]] = {
    0: ([4], " "),
    1: ([1, 2, 3, 4], "    "),
    2: ([1, 2, 3, 4], "    "),
    3: ([2, 3], "mm"),
    4: ([2, 3], "mm"),
    5: ([4], " "),
    6: ([2, 3, 1, 2, 3], "  kkk"),
    7: ([1, 2, 3], "   "),
    8: ([4], " "),
    9: ([2, 3, 1, 2, 3, 1, 2], "  kkkMM"),
    10: ([3], " "),
    11: ([1, 2], "  "),
    12: ([3, 4, 2, 3, 4, 5], "nnuuuu"),
    13: ([3, 4, 4, 5], "    "),
    14: ([2, 3, 3, 4], "mm  "),
    15: ([4, 5, 2, 3], "mm  "),
    16: ([2, 3], "  "),
    17: ([2, 3], "  "),
    18: ([1, 2], "mm"),
    19: ([1, 2], "mm"),
    20: ([3, 1, 2], "m  "),
    21: ([3, 1, 2], "m  "),
    22: ([3, 4, 4, 5], "    "),
    23: ([2, 3, 3, 4], "mm  "),
    24: ([4, 5, 2, 3], "mm  "),
    25: ([4], " "),
    26: ([4], " "),
}
_DEFAULT_RANGE = ([3], " ")

_SETUP_SUB_MODES = {100, 101, 105, 110, 120, 125, 130, 135, 140, 150, 160, 170, 175, 180, 190}

_DC_MODES = {M.LOW_Z, M.DCV, M.DC_MV, M.DC_UA, M.DC_MA, M.DC_A, M.DC_UVA, M.DC_MVA, M.DC_VA}
_AC_MODES = {M.ACV, M.AC_MV, M.AC_UA, M.AC_MA, M.AC_A, M.AC_UVA, M.AC_MVA, M.AC_VA}
_POWER_MODES = {M.AC_UVA, M.AC_MVA, M.AC_VA, M.DC_UVA, M.DC_MVA, M.DC_VA}

MODE_NAMES = {
    M.LOW_Z: "LowZ V", M.DCV: "DC V", M.ACV: "AC V", M.DC_MV: "DC mV", M.AC_MV: "AC mV",
    M.TEMP: "Temperature", M.HZ: "Frequency", M.MS: "Pulse width", M.DUTY: "Duty cycle",
    M.RESISTOR: "Resistance", M.CONTINUITY: "Continuity", M.DIODE: "Diode", M.CAPACITOR: "Capacitance",
    M.AC_UVA: "AC µVA", M.AC_MVA: "AC mVA", M.AC_VA: "AC VA",
    M.AC_UA: "AC µA", M.DC_UA: "DC µA", M.AC_MA: "AC mA", M.DC_MA: "DC mA", M.AC_A: "AC A", M.DC_A: "DC A",
    M.DC_UVA: "DC µVA", M.DC_MVA: "DC mVA", M.DC_VA: "DC VA",
    M.TEMP_C: "Temperature °C", M.TEMP_F: "Temperature °F", M.UNKNOWN: "Unknown",
}


def _safe(values: list[float], i: int, fallback: float) -> float:
    return values[i] if 0 <= i < len(values) else fallback


def _prefixed(base: str, notation: str) -> str:
    return {"n": "n", "u": "µ", "m": "m", "k": "k", "K": "k", "M": "M"}.get(notation, "") + base


def unit_string(mode: MeterMode, notation: str) -> str:
    if mode in (M.LOW_Z, M.DCV, M.ACV, M.DIODE):
        return _prefixed("V", notation)
    if mode in (M.DC_MV, M.AC_MV):
        return "mV"
    if mode in (M.TEMP, M.TEMP_C):
        return "°C"
    if mode == M.TEMP_F:
        return "°F"
    if mode == M.HZ:
        return _prefixed("Hz", notation)
    if mode == M.MS:
        return "ms"
    if mode == M.DUTY:
        return "%"
    if mode in (M.RESISTOR, M.CONTINUITY):
        return _prefixed("Ω", notation)
    if mode == M.CAPACITOR:
        return _prefixed("F", notation)
    if mode in (M.AC_UA, M.DC_UA):
        return "µA"
    if mode in (M.AC_MA, M.DC_MA):
        return "mA"
    if mode in (M.AC_A, M.DC_A):
        return "mA" if notation == "m" else "A"
    if mode in (M.AC_UVA, M.DC_UVA):
        return "mVA" if notation == "m" else "µVA"
    if mode in (M.AC_MVA, M.DC_MVA):
        return "mVA" if notation == "m" else _prefixed("VA", notation)
    if mode in (M.AC_VA, M.DC_VA):
        return _prefixed("VA", notation)
    return ""


def _f(value: float, digits: int) -> str:
    return f"{value:.{digits}f}"


class PacketV2:
    __slots__ = ("raw",)

    def __init__(self, raw: bytes):
        self.raw = bytes(raw)

    @staticmethod
    def checksum(frame: bytes) -> int:
        x = 0
        for b in frame[:18]:
            x ^= b
        return x

    @classmethod
    def try_parse(cls, data: bytes | bytearray | list[int]) -> PacketV2 | None:
        if len(data) < LENGTH or data[0] != START_BYTE:
            return None
        frame = bytes(data[:LENGTH])
        if cls.checksum(frame) != frame[18]:
            return None
        return cls(frame)

    # ---- raw helpers -------------------------------------------------------

    def _nib(self, i: int, high: bool) -> int:
        return (self.raw[i] >> 4) & 0x0F if high else self.raw[i] & 0x0F

    # ---- main reading ------------------------------------------------------

    @property
    def mode_raw(self) -> int:
        return self.raw[5] & 0x1F

    @property
    def mode(self) -> MeterMode:
        r = self.mode_raw
        return MeterMode(r) if r in _DEFINED_MODES else M.UNKNOWN

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, "Unknown")

    @property
    def is_resistance_mode(self) -> bool:
        return self.mode in (M.RESISTOR, M.CONTINUITY)

    @property
    def is_diode_mode(self) -> bool:
        return self.mode == M.DIODE

    @property
    def is_continuity_mode(self) -> bool:
        return self.mode == M.CONTINUITY

    @property
    def is_ac_millivolt(self) -> bool:
        return self.mode_raw == 4

    @property
    def is_dc_millivolt(self) -> bool:
        return self.mode_raw == 3

    @property
    def is_ac_volt(self) -> bool:
        return self.mode_raw == 2

    @property
    def is_dc_volt(self) -> bool:
        return self.mode_raw in (0, 1)

    @property
    def is_temperature(self) -> bool:
        if self.is_ac_millivolt or self.is_ac_volt:
            return False
        if self.icon_ac:
            return False
        if self.acdc_code in (2, 3):
            return False
        return self.mode_raw in (5, 25, 26)

    @property
    def is_setup_sub_mode(self) -> bool:
        return self.sub_mode_raw in _SETUP_SUB_MODES

    @property
    def dual_display_is_frequency(self) -> bool:
        if self.icon_dbm or self.is_setup_sub_mode:
            return False
        if self.icon_1khz or self.sub_mode_raw == 6:
            return True
        if (self.is_ac_millivolt or self.is_ac_volt) and self.sub_mode_raw in (0, 6):
            return True
        return False

    @property
    def is_power(self) -> bool:
        return self.mode in _POWER_MODES

    @property
    def is_main_overflow(self) -> bool:
        return bool(self._nib(6, True) & 0x8)

    @property
    def is_main_negative(self) -> bool:
        return bool(self._nib(6, True) & 0x4)

    @property
    def main_c(self) -> bool:
        return bool(self._nib(6, True) & 0x2)

    @property
    def main_f(self) -> bool:
        return bool(self._nib(6, True) & 0x1)

    @property
    def main_range_index(self) -> int:
        return self._nib(6, False)

    @property
    def main_int_value(self) -> int:
        hi = ((self.raw[5] >> 6) & 0x03) << 16
        return hi | (self.raw[7] << 8) | self.raw[8]

    @property
    def _spec(self) -> tuple[list[int], str]:
        return RANGES.get(int(self.mode), _DEFAULT_RANGE)

    @property
    def _clamped_range_index(self) -> int:
        return min(max(0, self.main_range_index), max(0, len(self._spec[0]) - 1))

    @property
    def main_range_value(self) -> int:
        pts = self._spec[0]
        if not pts:
            return 3
        return pts[min(self._clamped_range_index, len(pts) - 1)]

    @property
    def main_notation(self) -> str:
        n = self._spec[1]
        if not n:
            return " "
        return n[min(self._clamped_range_index, len(n) - 1)]

    @property
    def main_decimal_places(self) -> int:
        return max(0, 5 - self.main_range_value)

    @property
    def main_value(self) -> float:
        if self.is_main_overflow:
            return math.nan
        v = self.main_int_value / (10.0 ** self.main_decimal_places)
        return -v if self.is_main_negative else v

    @property
    def range_multiple(self) -> float:
        return {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6}.get(self.main_notation, 1.0)

    @property
    def si_value(self) -> float:
        v = self.main_value
        return v * self.range_multiple if math.isfinite(v) else v

    @property
    def formatted_main_value(self) -> str:
        if self.is_main_overflow:
            return "OL"
        s = f"{abs(self.main_int_value) % 100_000:05d}"
        cut = min(max(self.main_range_value, 1), 5)
        if cut < 5:
            s = s[:cut] + "." + s[cut:]
        if "." in s:
            chars = list(s)
            i = 0
            while i < len(chars) - 1 and chars[i] == "0" and chars[i + 1] != ".":
                chars[i] = " "
                i += 1
            return "".join(chars)
        return s

    @property
    def main_unit(self) -> str:
        if self.is_temperature:
            return "°F" if (self.mode_raw == 26 or (self.main_f and not self.main_c)) else "°C"
        if self.is_ac_millivolt or self.is_dc_millivolt:
            return "mV"
        if self.mode in (M.DCV, M.ACV, M.LOW_Z) and self.main_notation == "m":
            return "mV"
        return unit_string(self.mode, self.main_notation)

    @property
    def display_string(self) -> str:
        if self.is_main_overflow:
            return f"OL {self.main_unit}".strip()
        sign = "-" if self.is_main_negative else ""
        return f"{sign}{self.formatted_main_value.strip()} {self.main_unit}".strip()

    @property
    def measure_fingerprint(self) -> str:
        return f"{self.mode_raw}:{self.acdc_code}"

    # ---- bar graph ---------------------------------------------------------

    @property
    def bar_is_used(self) -> bool:
        return (self.raw[13] & 0x10) == 0

    @property
    def bar_uses_150_scale(self) -> bool:
        return bool(self.raw[13] & 0x08)

    @property
    def bar_is_negative(self) -> bool:
        return bool(self.raw[13] & 0x04)

    @property
    def bar_range_index(self) -> int:
        return self.raw[13] & 0x03

    @property
    def bar_nominal_scale(self) -> float:
        return {0: 5.0, 1: 50.0, 2: 500.0}.get(self.bar_range_index, 1000.0)

    @property
    def analog_bar_raw(self) -> int:
        return self.raw[14] & 0x1F

    # ---- sub display -------------------------------------------------------

    @property
    def sub_mode_raw(self) -> int:
        return self.raw[9]

    @property
    def is_sub_overflow(self) -> bool:
        return bool(self._nib(10, True) & 0x8)

    @property
    def is_sub_negative(self) -> bool:
        return bool(self._nib(10, True) & 0x4)

    @property
    def sub_point(self) -> int:
        return self._nib(10, False)

    @property
    def sub_int_value(self) -> int:
        return (self.raw[11] << 8) | self.raw[12]

    @property
    def sub_value(self) -> float:
        if self.is_sub_overflow:
            return math.nan
        p = min(max(self.sub_point, 0), 6)
        v = self.sub_int_value / (10.0 ** p)
        return -v if self.is_sub_negative else v

    # ---- icons -------------------------------------------------------------

    @property
    def icon1(self) -> int:
        return self.raw[15]

    @property
    def icon2(self) -> int:
        return self.raw[16]

    @property
    def icon3(self) -> int:
        return self.raw[17]

    icon_1khz = property(lambda s: bool(s.icon1 & 0x40))
    icon_1ms = property(lambda s: bool(s.icon1 & 0x20))
    icon_auto = property(lambda s: bool(s.icon1 & 0x04))
    icon_apo = property(lambda s: bool(s.icon1 & 0x02))
    icon_bat = property(lambda s: bool(s.icon1 & 0x01))
    icon_bt = property(lambda s: bool(s.icon2 & 0x40))
    icon_rel = property(lambda s: bool(s.icon2 & 0x10))
    icon_dbm = property(lambda s: bool(s.icon2 & 0x08))
    icon_min_max = property(lambda s: bool(s.icon2 & 0x07))
    icon_test = property(lambda s: bool(s.icon3 & 0x40))
    icon_mem = property(lambda s: bool(s.icon3 & 0x30))
    icon_hold = property(lambda s: bool((s.icon3 >> 2) & 0x3))
    # HOLD key cycles off -> HOLD (2) -> Auto-HOLD (1) -> off; Auto-HOLD lights an extra "A" segment
    icon_auto_hold = property(lambda s: ((s.icon3 >> 2) & 0x3) == 1)
    icon_ac = property(lambda s: bool(s.icon3 & 0x02))
    icon_dc = property(lambda s: bool(s.icon3 & 0x01))
    icon_cont = property(lambda s: s.is_continuity_mode)
    icon_low_z = property(lambda s: s.mode == M.LOW_Z)

    @property
    def min_max_label(self) -> str:
        """Annunciator text for the min-max bits (mapping from the original EEVblog app)."""
        return {1: "MAX", 2: "MIN", 3: "AVG", 4: "MAX MIN AVG"}.get(self.icon2 & 0x07, "")

    @property
    def bar_segments(self) -> int:
        """Number of lit analog bargraph segments (0..26)."""
        return min(26, self.analog_bar_raw + 1) if self.bar_is_used else 0

    @property
    def acdc_code(self) -> int:
        return (self.icon1 >> 3) & 0x3

    @property
    def icon_acdc(self) -> bool:
        return self.acdc_code == 3 or (self.icon_dc and self.icon_ac)

    @property
    def is_dc_mode(self) -> bool:
        return self.mode in _DC_MODES

    @property
    def is_ac_mode(self) -> bool:
        return self.mode in _AC_MODES

    @property
    def coupling(self) -> Coupling:
        code = self.acdc_code
        if code == 3:
            return Coupling.ACDC
        if code == 2:
            return Coupling.AC
        if code == 1:
            return Coupling.DC
        if self.icon_dc and self.icon_ac:
            return Coupling.ACDC
        if self.icon_ac:
            return Coupling.AC
        if self.icon_dc:
            return Coupling.DC
        if self.is_ac_mode:
            return Coupling.AC
        if self.is_dc_mode:
            return Coupling.DC
        return Coupling.NONE

    @property
    def sub_is_temperature(self) -> bool:
        if self.is_setup_sub_mode:
            return self.sub_mode_raw in (100, 105)
        if self.dual_display_is_frequency or self.is_ac_millivolt or self.is_ac_volt:
            return False
        return self.sub_mode_raw in (5, 25, 26)

    @property
    def sub_unit(self) -> str:
        if self.icon_dbm:
            return "dBm"
        if self.dual_display_is_frequency:
            return "kHz" if (self.icon_1khz or abs(self.sub_value) >= 1000) else "Hz"
        s = self.setup_sub_presentation()
        if s is not None:
            return s.unit or s.label
        if self.sub_is_temperature:
            return "°F" if self.sub_mode_raw in (26, 105) else "°C"
        sm = self.sub_mode_raw
        if sm == 0:
            return ""
        if sm < 25 and (sm & 0x1F) in _DEFINED_MODES:
            return unit_string(MeterMode(sm & 0x1F), " ")
        return ""

    @property
    def continuity_beeper_ohms(self) -> float | None:
        if not (self.is_continuity_mode or self.is_resistance_mode):
            return None
        if self.sub_mode_raw in (0, 5, 6, 9, 10, 25, 26, 100, 105, 110, 120, 125, 130, 135, 140, 150, 160, 180, 190):
            return None
        n_sub = abs(self.sub_int_value)
        n_main = abs(self.main_int_value)
        if n_sub in (30, 300):
            return float(n_sub)
        if n_main in (30, 300):
            return float(n_main)
        return None

    @property
    def measurement_position(self) -> MeasurementPosition:
        m = self.mode
        if m in (M.LOW_Z, M.DCV, M.ACV):
            return MeasurementPosition.VOLTAGE
        if m in (M.DC_MV, M.AC_MV, M.TEMP, M.TEMP_C, M.TEMP_F):
            return MeasurementPosition.MILLIVOLT_TEMPERATURE
        if m in (M.RESISTOR, M.CONTINUITY, M.DIODE, M.CAPACITOR):
            return MeasurementPosition.RESISTANCE
        if m in (M.HZ, M.MS, M.DUTY):
            return MeasurementPosition.FREQUENCY
        if m in (M.AC_MA, M.DC_MA, M.AC_A, M.DC_A):
            return MeasurementPosition.CURRENT
        if m in (M.AC_UA, M.DC_UA, M.AC_UVA, M.DC_UVA):
            return MeasurementPosition.MICRO_CURRENT
        if m in (M.AC_MVA, M.DC_MVA, M.AC_VA, M.DC_VA):
            return MeasurementPosition.POWER_CURRENT
        return MeasurementPosition.UNKNOWN

    def setup_sub_presentation(self) -> SubPresentation | None:
        sm = self.sub_mode_raw
        if self.icon_dbm and not self.is_setup_sub_mode:
            v = self.sub_value if math.isfinite(self.sub_value) else abs(self.sub_int_value) / 10.0
            return SubPresentation(_f(v, 1), "dBm", "dBm")
        if sm in (120, 125) and self.mode in (
            M.CAPACITOR, M.HZ, M.MS, M.DUTY, M.RESISTOR, M.CONTINUITY, M.DIODE,
            M.AC_UVA, M.AC_MVA, M.AC_VA, M.DC_UVA, M.DC_MVA, M.DC_VA,
        ):
            return None

        n_sub = abs(self.sub_int_value)
        n_main = abs(self.main_int_value)
        n = n_sub if n_sub != 0 else n_main
        v_sub = self.sub_value
        v_main = self.main_value
        if math.isfinite(v_sub) and abs(v_sub) > 0:
            v = abs(v_sub)
        elif math.isfinite(v_main):
            v = abs(v_main)
        else:
            v = float(n)

        if sm in (5, 25, 100):
            return SubPresentation(_f(v, 1), "°C", "Temperature")
        if sm == 26:
            if self.is_ac_millivolt or self.is_ac_volt:
                return self._format_sub_frequency()
            return SubPresentation(_f(v, 1), "°F", "Temperature")
        if sm == 105:
            return SubPresentation(_f(v, 1), "°F", "Temperature")
        if sm == 110:
            return SubPresentation(_f(v, 1), "V", "Battery")
        if sm == 120:
            return SubPresentation("on", "", "Auto Power OFF")
        if sm == 125:
            return SubPresentation("oFF", "", "Auto Power OFF")
        if sm == 101:
            return SubPresentation("oFF" if n == 0 else "on", "", "BEEP")
        if sm == 130:
            y = n % 100_000
            if y < 100:
                y += 2000
            if 2099 < y <= 20_999:
                y //= 10
            if y < 1990 or y > 2099:
                y = 2000 + (n % 100)
            return SubPresentation(f"{y:04d}", "", "YEAR")
        if sm == 135:
            return SubPresentation(f"{min(12, (n >> 8) & 0xFF):02d}-{min(31, n & 0xFF):02d}", "", "DATE")
        if sm == 140:
            return SubPresentation(f"{min(23, (n >> 8) & 0xFF):02d}-{min(59, n & 0xFF):02d}", "", "TIME")
        if sm == 150 and self.measurement_position in (MeasurementPosition.CURRENT, MeasurementPosition.MICRO_CURRENT):
            if self.sub_point == 0 and n == 0:
                return SubPresentation("bd.0FF", "", "BURDEN")
            if self.sub_point == 0 and n == 1:
                return SubPresentation("bd.0n", "", "BURDEN")
            return SubPresentation(_f(v, 1), "mV", "BURDEN")
        if sm == 160:
            return SubPresentation(f"{n % 10}", "", "LCD Contrast")
        if sm in (170, 175):
            return SubPresentation(f"{n % 100_000:05d}", "", "Calibration")
        if sm == 180:
            dv = v_sub if math.isfinite(v_sub) else n / 10.0
            return SubPresentation(_f(dv, 1), "dBm", "dBm")
        if sm == 190:
            return SubPresentation(f"{n % 100_000:05d}", "", "In")
        if sm == 6:
            return self._format_sub_frequency()
        if self.dual_display_is_frequency:
            return self._format_sub_frequency()
        t = self.continuity_beeper_ohms
        if t is not None:
            return SubPresentation(f"{t:.0f}", "Ω", "BEEP")
        return None

    def _format_sub_frequency(self) -> SubPresentation:
        raw_n = abs(self.sub_int_value)
        v = self.sub_value
        if not math.isfinite(v):
            v = float(raw_n)
        d = min(max(self.sub_point, 0), 4)
        if v == 0 and raw_n == 0:
            return SubPresentation("0", "Hz", "Hz")
        if self.icon_1khz or abs(v) >= 1000:
            shown = abs(v) / 1000.0 if abs(v) >= 1000 else abs(v)
            return SubPresentation(_f(shown, d), "kHz", "Hz")
        return SubPresentation(_f(abs(v), d), "Hz", "Hz")

    @property
    def formatted_sub_value(self) -> str:
        s = self.setup_sub_presentation()
        if s is not None:
            return s.digits if not s.unit else f"{s.digits} {s.unit}"
        if self.is_sub_overflow:
            return "OL"
        v = self.sub_value
        if math.isnan(v):
            return "-"
        num = _f(v, min(max(self.sub_point, 0), 4))
        unit = self.sub_unit
        return f"{num} {unit}" if unit else num

    def setup_descriptor(self) -> SetupDescriptor | None:
        sm = self.sub_mode_raw
        n = abs(self.sub_int_value)
        if sm == 100:
            return SetupDescriptor(SetupItem.TEMPERATURE, "Temperature", "°C")
        if sm == 105:
            return SetupDescriptor(SetupItem.TEMPERATURE, "Temperature", "°F")
        if sm == 101:
            return SetupDescriptor(SetupItem.BUZZER, "Buzzer", "OFF" if n == 0 else "ON")
        if sm == 110:
            return SetupDescriptor(SetupItem.BATTERY, "Battery", self._fmt_battery_setup())
        if sm == 120:
            return SetupDescriptor(SetupItem.AUTO_POWER_OFF, "Auto Power Off", "ON")
        if sm == 125:
            return SetupDescriptor(SetupItem.AUTO_POWER_OFF, "Auto Power Off", "OFF")
        if sm == 130:
            y = n % 100_000
            if y < 100:
                y += 2000
            if y < 1990 or y > 2099:
                y = 2000 + (n % 100)
            return SetupDescriptor(SetupItem.YEAR, "Year", f"{y:04d}")
        if sm == 135:
            return SetupDescriptor(SetupItem.DATE, "Date", f"{min(12, (n >> 8) & 0xFF):02d}-{min(31, n & 0xFF):02d}")
        if sm == 140:
            return SetupDescriptor(SetupItem.TIME, "Time", f"{min(23, (n >> 8) & 0xFF):02d}:{min(59, n & 0xFF):02d}")
        if sm == 150 and self.mode in (M.AC_MA, M.DC_MA, M.AC_A, M.DC_A, M.AC_UA, M.DC_UA):
            return SetupDescriptor(SetupItem.BURDEN_VOLTAGE, "Burden voltage", self._fmt_burden_setup())
        if sm == 160:
            return SetupDescriptor(SetupItem.LCD_CONTRAST, "LCD contrast", f"{n % 10}")
        if sm in (170, 175):
            if self.icon_test:
                return SetupDescriptor(SetupItem.UNKNOWN, "Calibration", f"{n % 100_000:05d}")
            return SetupDescriptor(SetupItem.LOGGING_INTERVAL, "Logging interval", f"In {n}")
        if sm == 190:
            return SetupDescriptor(SetupItem.METER_ID, "Multimeter ID", f"{n % 100_000:05d}")
        if self.mode == M.CONTINUITY:
            ohms = self.continuity_beeper_ohms
            if ohms is not None:
                return SetupDescriptor(SetupItem.CONTINUITY_THRESHOLD, "Continuity threshold", f"{ohms:.0f} Ω")
        return None

    def _fmt_battery_setup(self) -> str:
        if math.isfinite(self.sub_value):
            return f"{self.sub_value:.2f} V"
        if math.isfinite(self.main_value):
            return f"{self.main_value:.2f} V"
        return "—"

    def _fmt_burden_setup(self) -> str:
        n = abs(self.sub_int_value)
        if self.sub_point == 0 and n == 0:
            return "OFF"
        if self.sub_point == 0 and n == 1:
            return "ON"
        v = self.sub_value if math.isfinite(self.sub_value) else self.main_value
        return f"{v:.1f} mV" if math.isfinite(v) else "—"

    # ---- graph normalisation -----------------------------------------------

    @property
    def graph_unit(self) -> str:
        m = self.mode
        if m in (M.LOW_Z, M.DCV, M.ACV, M.DIODE):
            return "V"
        if m in (M.DC_MV, M.AC_MV):
            return "mV"
        if m in (M.TEMP, M.TEMP_C):
            return "°C"
        if m == M.TEMP_F:
            return "°F"
        if m == M.HZ:
            return "Hz"
        if m == M.MS:
            return "ms"
        if m == M.DUTY:
            return "%"
        if m in (M.RESISTOR, M.CONTINUITY):
            return "Ω"
        if m == M.CAPACITOR:
            return "µF"
        if m in (M.AC_UA, M.DC_UA):
            return "µA"
        if m in (M.AC_MA, M.DC_MA):
            return "mA"
        if m in (M.AC_A, M.DC_A):
            return "A"
        if m in (M.AC_UVA, M.DC_UVA):
            return "µVA"
        if m in (M.AC_MVA, M.DC_MVA):
            return "mVA"
        if m in (M.AC_VA, M.DC_VA):
            return "VA"
        return self.main_unit

    @property
    def graph_value(self) -> float:
        v = self.main_value
        if not math.isfinite(v):
            return math.nan
        m = self.mode
        si = self.si_value
        if m in (M.LOW_Z, M.DCV, M.ACV, M.DIODE, M.RESISTOR, M.CONTINUITY, M.AC_A, M.DC_A, M.AC_VA, M.DC_VA):
            return si
        if m in (M.DC_MV, M.AC_MV, M.AC_MA, M.DC_MA, M.AC_MVA, M.DC_MVA):
            return si * 1000.0
        if m == M.CAPACITOR:
            return si * 1_000_000.0
        return v

    @property
    def graph_full_scale(self) -> float:
        i = max(0, self.main_range_index)
        fb = self._fallback_full_scale()
        m = self.mode
        table: dict[tuple[MeterMode, ...], list[float]] = {
            (M.LOW_Z, M.DCV, M.ACV): [5, 50, 500, 1000],
            (M.DC_MV, M.AC_MV): [50, 500],
            (M.HZ,): [100, 1000, 10_000, 100_000, 1_000_000],
            (M.MS,): [10, 100, 1000],
            (M.RESISTOR,): [50, 500, 5_000, 50_000, 500_000, 5_000_000, 50_000_000],
            (M.DIODE,): [3, 15],
            (M.CAPACITOR,): [0.01, 0.1, 1, 10, 100, 9999],
            (M.AC_UA, M.DC_UA): [50, 500],
            (M.AC_MA, M.DC_MA): [5, 50],
            (M.AC_A, M.DC_A): [0.5, 5.0, 10.0],
            (M.AC_UVA, M.DC_UVA): [250, 2500, 25_000],
            (M.AC_MVA, M.DC_MVA): [25, 250, 2500, 25_000],
            (M.AC_VA, M.DC_VA): [50, 500],
        }
        if m in (M.TEMP, M.TEMP_C):
            return 1350.0
        if m == M.TEMP_F:
            return 2450.0
        if m == M.DUTY:
            return 100.0
        if m == M.CONTINUITY:
            return 50.0
        for modes, values in table.items():
            if m in modes:
                return float(_safe(values, i, fb))
        return fb

    @property
    def graph_lower_bound(self) -> float:
        if self.mode in (M.RESISTOR, M.CONTINUITY, M.DIODE, M.CAPACITOR, M.HZ, M.MS, M.DUTY):
            return 0.0
        return -self.graph_full_scale

    def _fallback_full_scale(self) -> float:
        p = max(0, self.main_range_value)
        display = 50_000.0 / (10.0 ** max(0, 5 - p))
        scaled = display * self.range_multiple
        unit = self.graph_unit
        if unit in ("mV", "mA", "mVA"):
            return max(abs(scaled * 1000), 1e-12)
        if unit in ("µA", "µVA", "µF"):
            return max(abs(scaled * 1_000_000), 1e-12)
        return max(abs(scaled), 1e-12)

    def __repr__(self) -> str:
        return f"<PacketV2 {self.mode_name} {self.display_string!r} sub={self.formatted_sub_value!r}>"


class FrameAssembler:
    """Reassembles 0xF2 frames from an arbitrary BLE byte stream."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[PacketV2]:
        if not data:
            return []
        self.buffer.extend(data)
        if len(self.buffer) > 1024:
            del self.buffer[: len(self.buffer) - 256]
        out: list[PacketV2] = []
        offset = 0
        buf = self.buffer
        while offset + LENGTH <= len(buf):
            if buf[offset] != START_BYTE:
                offset += 1
                continue
            pkt = PacketV2.try_parse(buf[offset : offset + LENGTH])
            if pkt is not None:
                out.append(pkt)
                offset += LENGTH
            else:
                offset += 1
        if offset:
            del buf[:offset]
        return out

    def clear(self) -> None:
        self.buffer.clear()


def build_frame(
    mode: int,
    value: int = 0,
    range_index: int = 0,
    *,
    negative: bool = False,
    overflow: bool = False,
    temp_c: bool = False,
    temp_f: bool = False,
    sub_mode: int = 0,
    sub_value: int = 0,
    sub_point: int = 0,
    sub_negative: bool = False,
    sub_overflow: bool = False,
    bar_flags: int = 0x10,
    bar_value: int = 0,
    icon1: int = 0,
    icon2: int = 0,
    icon3: int = 0,
    serial: bytes = b"\x00\x00\x00\x00",
) -> bytes:
    """Build a valid frame. Used by the demo meter and the tests."""
    value &= 0x3FFFF
    raw = bytearray(LENGTH)
    raw[0] = START_BYTE
    raw[1:5] = serial[:4].ljust(4, b"\x00")
    raw[5] = (mode & 0x1F) | (((value >> 16) & 0x03) << 6)
    flags = (8 if overflow else 0) | (4 if negative else 0) | (2 if temp_c else 0) | (1 if temp_f else 0)
    raw[6] = (flags << 4) | (range_index & 0x0F)
    raw[7] = (value >> 8) & 0xFF
    raw[8] = value & 0xFF
    raw[9] = sub_mode & 0xFF
    sflags = (8 if sub_overflow else 0) | (4 if sub_negative else 0)
    raw[10] = (sflags << 4) | (sub_point & 0x0F)
    raw[11] = (sub_value >> 8) & 0xFF
    raw[12] = sub_value & 0xFF
    raw[13] = bar_flags & 0xFF
    raw[14] = bar_value & 0xFF
    raw[15] = icon1 & 0xFF
    raw[16] = icon2 & 0xFF
    raw[17] = icon3 & 0xFF
    raw[18] = PacketV2.checksum(raw)
    return bytes(raw)
