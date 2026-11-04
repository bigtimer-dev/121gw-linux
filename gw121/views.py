"""The display views of zombu4's open firmware (v0.9.6 and later), replayed in the app.

The meter's SETUP pages `LP` (DC mA as percent of a 4-20 / 0-20 mA loop) and `Pt` (Ω as the temperature of a PT100 /
PT1000) change only the meter's own display: the Bluetooth frame keeps the current and the resistance and does not say
which view is on. The app therefore has a matching setting of its own (main menu, Meter Views). As on the meter, a view
changes only the main reading: the chart, the log and the CSV export keep the current / resistance.

The arithmetic is the firmware's (ui/loop.s, ui/rtd.s, mtmp/temp.s fn_08013B76), on the same counts, so the app shows
the same digits as the meter: the same RTD table (R / R0 in ppm every 5 °C, IEC 60751, built as
tools/gen_rtd_table.py builds it), the same interpolation and the same half-away-from-zero roundings.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .packet import MeterMode, PacketV2

LOOP_VIEWS = ("off", "4-20", "0-20")
RTD_VIEWS = ("off", "pt100", "pt1000")

_A, _B, _C = Fraction(39083, 10 ** 7), Fraction(-5775, 10 ** 10), Fraction(-4183, 10 ** 15)
_T_LO, _T_STEP = -20000, 500                     # 0.01 °C: first entry, step
_T_MIN, _T_MAX = -20004, 85004                   # shown span: -200.04 .. 850.04 °C (rounds to -200.0 .. 850.0)
_RTD_RANGE_MAX = 3                               # OHM 50.000 kΩ: the highest range converted


def _round_half_away(q: Fraction) -> int:
    n = abs(q.numerator) * 2 + q.denominator
    v = n // (2 * q.denominator)
    return v if q >= 0 else -v


def _w(t: int) -> Fraction:
    t = Fraction(t)
    v = 1 + _A * t + _B * t * t
    if t < 0:
        v += _C * (t - 100) * t ** 3
    return v


RTD_TABLE = tuple(_round_half_away(_w(t) * 10 ** 6) for t in range(-200, 851, 5))   # 211 entries, ppm


def _tdiv(a: int, b: int) -> int:
    """Integer division truncating toward zero (the Cortex-M sdiv)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


@dataclass(frozen=True)
class ViewReading:
    text: str          # the main digits as the meter shows them, e.g. " 50.00", " 100.0", " OFL "
    unit: str          # "%", "°C" or "°F"
    negative: bool     # the minus sign
    label: str         # e.g. "4–20 mA loop", "PT100"


def loop_hundredths(counts: int, range_index: int, view: str) -> int | None:
    """Loop percent in 0.01 % from the signed DCmA reading (ui_loop_draw); None for an unknown range."""
    if range_index == 0:
        tenth_ua = counts                        # 5.0000 mA: 0.1 µA
    elif range_index == 1:
        tenth_ua = counts * 10                   # 50.000 mA: 1 µA
    else:
        return None
    if view == "4-20":
        x, div = tenth_ua - 40000, 16
    else:
        x, div = tenth_ua, 20
    mag = (abs(x) + div // 2) // div
    return -mag if x < 0 else mag


def rtd_hundredths(counts: int, range_index: int, view: str) -> int | str:
    """RTD temperature in 0.01 °C from the signed OHM reading (ui_rtd_draw), or "over" / "under"."""
    if range_index < 0 or range_index > _RTD_RANGE_MAX:
        return "over"
    ppm = counts * 10 ** range_index             # 1 mΩ
    if view == "pt100":
        ppm *= 10                                # 1 mΩ = 10 ppm of 100 Ω (Pt1000: 1 ppm of 1000 Ω)
    tab = RTD_TABLE
    if ppm < tab[0]:
        if ppm < tab[0] - (tab[1] - tab[0]):
            return "under"
        k = 0
    elif ppm > tab[-1]:
        if ppm > tab[-1] + (tab[-1] - tab[-2]):
            return "over"
        k = len(tab) - 2
    else:
        k = 0
        while ppm > tab[k + 1]:
            k += 1
    d = tab[k + 1] - tab[k]
    x = (ppm - tab[k]) * _T_STEP
    x = x + d // 2 if x >= 0 else x - d // 2
    t = _tdiv(x, d) + _T_STEP * k + _T_LO
    if t < _T_MIN:
        return "under"
    if t > _T_MAX:
        return "over"
    return t


def to_fahrenheit_hundredths(t: int) -> int:
    """fn_08013B76: 0.01 °C to 0.01 °F, (9 t + 16000) / 5 rounded half away from zero."""
    n = 9 * t + 16000
    return _tdiv(n + 2 if n >= 0 else n - 2, 5)


def to_tenths(t: int) -> int:
    """0.01 degree to 0.1 degree, half away from zero (fn_08007840, OF-21)."""
    return _tdiv(t + 5 if t >= 0 else t - 5, 10)


def _digits(value: int, decimals: int) -> str:
    s = f"{abs(value) / 10 ** decimals:.{decimals}f}"
    return s.rjust(6)


def view_shown(p: PacketV2, loop_view: str, rtd_view: str) -> str | None:
    """Which view the meter would show for this frame: "loop", "rtd" or None (ui_loop_view, ui_rtd_view)."""
    if p.is_main_overflow or p.icon_test or p.icon_mem:
        return None                              # overload, calibration, logging or the MEM view: the reading
    if p.mode == MeterMode.DC_MA and loop_view in ("4-20", "0-20") and not p.icon_rel:
        return "loop"
    if p.mode == MeterMode.RESISTOR and rtd_view in ("pt100", "pt1000"):
        return "rtd"
    return None


def view_for(p: PacketV2, loop_view: str, rtd_view: str, temp_unit: str = "°C") -> ViewReading | None:
    """The reading the meter shows with these settings, or None when it shows the normal reading."""
    which = view_shown(p, loop_view, rtd_view)
    if which is None:
        return None
    counts = -p.main_int_value if p.is_main_negative else p.main_int_value
    if which == "loop":
        v = loop_hundredths(counts, p.main_range_index, loop_view)
        if v is None:
            return None
        label = "4–20 mA loop" if loop_view == "4-20" else "0–20 mA loop"
        return ViewReading(_digits(v, 2), "%", v < 0, label)
    label = "PT100" if rtd_view == "pt100" else "PT1000"
    unit = "°F" if temp_unit == "°F" else "°C"
    t = rtd_hundredths(counts, p.main_range_index, rtd_view)
    if isinstance(t, str):
        return ViewReading("  OFL ", unit, t == "under", label)
    if unit == "°F":
        t = to_fahrenheit_hundredths(t)
    v = to_tenths(t)
    return ViewReading(_digits(v, 1), unit, v < 0, label)
