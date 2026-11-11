"""Go straight to a meter state from a list: the app presses the key, waits until the meter shows the result, and
presses again until the chosen page, function or range is reached.

The orders come from the open firmware (firmware/src/mode/cycle.s, set/setup.s fn_08015410); the navigation does not
depend on them being exact: it watches what the meter reports after every press and stops when the goal is met, when
the meter does not react, or after the most presses the list can need.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib

from .card import custom_key, setpage_type
from .keys import MeterKey
from .packet import MODE_NAMES, RANGES, Coupling, MeterMode as M, unit_string
from .setup_pages import PAGES

STEP_WAIT_S = 3.0           # the meter shows a key's effect in the next frame (2 frames/s at the slowest rate)


@dataclass(frozen=True)
class Option:
    label: str
    detail: str
    goal: Callable[[object], bool]      # link -> reached
    current: bool = False
    long_press: bool = False            # one hold of the key does it (RANGE auto, MIN/MAX stop)
    blocked: str = ""                   # why the app cannot get there (shown, not selectable)
    item: int | None = None             # SETUP: the page (for the firmware's own jump, CF-09)


# ---- MODE: the cycles of the dial positions (cycle.s cycle_mode_on_bit0) ------------------------------------------

_V = "V"
MODE_GROUPS: list[list[tuple[int, str | None]]] = [
    [(M.DCV, None), (M.ACV, "AC"), (M.ACV, "ACDC")],
    [(M.DC_MV, None), (M.AC_MV, None), (M.TEMP, None)],
    [(M.HZ, None), (M.MS, None), (M.DUTY, None)],
    [(M.RESISTOR, None), (M.CONTINUITY, None), (M.DIODE, None), (M.CAPACITOR, None)],
    [(M.AC_UVA, None), (M.DC_UA, None), (M.AC_UA, None), (M.DC_UVA, None)],
    [(M.AC_MVA, None), (M.DC_MVA, None)],
    [(M.AC_VA, None), (M.DC_VA, None)],
    [(M.AC_MA, None), (M.DC_MA, None)],
    [(M.AC_A, None), (M.DC_A, None)],
]


def _mode_state(p) -> tuple[int, str | None]:
    m = int(p.mode)
    if m in (M.TEMP_C, M.TEMP_F):
        m = int(M.TEMP)
    if m == M.ACV:
        return m, "ACDC" if p.coupling == Coupling.ACDC else "AC"
    return m, None


def _mode_label(mode: int, variant: str | None) -> str:
    if mode == M.ACV:
        return "AC+DC V" if variant == "ACDC" else "AC V"
    return MODE_NAMES.get(M(mode), str(mode))


def mode_options(p) -> list[Option]:
    if p is None:
        return []
    now = _mode_state(p)
    for group in MODE_GROUPS:
        if now in group:
            return [Option(_mode_label(*s), "", (lambda link, s=s: link.latest is not None
                                                  and _mode_state(link.latest) == s), current=(s == now))
                    for s in group]
    return []


# ---- RANGE ---------------------------------------------------------------------------------------------------------

def range_label(mode: int, k: int) -> str:
    if mode == M.DIODE:
        return ("3 V test voltage", "15 V test voltage")[k] if k < 2 else f"Range {k + 1}"
    if mode in (M.DCV, M.ACV, M.DC_MV, M.AC_MV, M.RESISTOR):
        dps, notation = RANGES[mode]
        if mode in (M.DCV, M.ACV) and k == len(dps) - 1:
            return "600 V"
        top = 5 * 10 ** (dps[k] - 1)
        return f"{top} {unit_string(M(mode), notation[k])}"
    return f"Range {k + 1}"


def range_options(p) -> list[Option]:
    if p is None or int(p.mode) not in RANGES or len(RANGES[int(p.mode)][0]) < 2:
        return []
    mode, n = int(p.mode), len(RANGES[int(p.mode)][0])
    auto = bool(p.icon_auto)
    opts = [Option("Auto", "the meter picks the range (hold RANGE)",
                   lambda link: link.latest is not None and bool(link.latest.icon_auto), current=auto,
                   long_press=True)]
    for k in range(n):
        opts.append(Option(range_label(mode, k), "fixed range",
                           (lambda link, k=k: link.latest is not None and not link.latest.icon_auto
                            and link.latest.main_range_index == k),
                           current=(not auto and p.main_range_index == k)))
    return opts


# ---- HOLD and MIN/MAX ---------------------------------------------------------------------------------------------

def _hold_state(p) -> int:
    return (p.icon3 >> 2) & 0x3            # 0 off, 2 HOLD, 1 auto-hold


def hold_options(p) -> list[Option]:
    if p is None:
        return []
    now = _hold_state(p)
    states = [(0, "Off", ""), (2, "HOLD", "the reading frozen")]
    if int(p.mode) not in (M.HZ, M.MS, M.DUTY, M.CAPACITOR) and not p.icon_min_max:
        states.append((1, "Auto-hold", "keeps each steady reading"))
    return [Option(label, detail, (lambda link, s=s: link.latest is not None and _hold_state(link.latest) == s),
                   current=(s == now)) for s, label, detail in states]


def minmax_options(p) -> list[Option]:
    if p is None:
        return []
    now = p.icon2 & 0x07
    opts = [Option(label, detail, (lambda link, s=s: link.latest is not None and link.latest.icon2 & 0x07 == s),
                   current=(s == now))
            for s, label, detail in ((1, "MAX", "the highest reading"), (2, "MIN", "the lowest reading"),
                                     (3, "AVG", "the average"), (4, "MAX MIN AVG", "all three"))]
    opts.append(Option("Off", "stop MIN/MAX (hold)", lambda link: link.latest is not None
                       and not link.latest.icon2 & 0x07, current=(now == 0), long_press=True))
    return opts


# ---- SETUP (set/setup.s fn_08015410) -------------------------------------------------------------------------------

def setup_item(link) -> int:
    st = link.setup_text
    return st.item if st is not None else 0


def setup_pages(mode: int | None, minmax: bool = False, has_bootloader: bool = False) -> list[int]:
    """The SETUP items in the order the SETUP key steps through them for `mode` (pages that only appear in some
    states, such as Ch and the limit verdict, are left out; the navigation still steps over them)."""
    last = 10
    if mode == M.ACV:
        last = 12
    elif mode in (M.AC_UVA, M.AC_MVA, M.AC_VA, M.DC_UVA, M.DC_MVA, M.DC_VA, M.AC_UA, M.AC_MA, M.AC_A,
                  M.DC_UA, M.DC_MA, M.DC_A):
        last = 12 if mode in (M.AC_UA, M.AC_MA, M.AC_A) else 11
    elif mode in (M.AC_MV, M.CONTINUITY, M.DIODE):
        last = 11
    items = list(range(1, last)) + [20, 21, 39]
    if has_bootloader:
        items.append(40)
    items.append(22)
    if mode in (M.TEMP, M.TEMP_C, M.TEMP_F):
        items.append(23)
    items.append(24)
    extra = {M.CONTINUITY: 26, M.DC_MA: 37, M.RESISTOR: 38}.get(M(mode) if mode is not None else None)
    if extra:
        items.append(extra)
    items += [25, 27, 28, 29, 30]
    if minmax:
        items += [32, 33, 34, 35]
    return items


PAGE_EXTRA = {40: ("For", "Format the card", "Formats the SD card as exFAT (two-part bootloader only)."),
              10: ("", "Function option", "Depends on the function (continuity threshold, burden voltage, …)."),
              11: ("", "Function option", "Depends on the function (dBm in AC V, …)."),
              12: ("", "Function option", "The power functions' extra value.")}


def page_label(item: int) -> tuple[str, str]:
    short, title, _ = PAGES.get(item) or PAGE_EXTRA.get(item) or ("", f"Page {item}", "")
    return (f"{short} · {title}" if short else title), ""


def setup_options(link) -> list[Option]:
    p = link.latest
    if p is None and link.setup_text is None:
        return []
    now = 39 if link.on_ld_page else setup_item(link)
    direct = supports_goto(link)
    mode = int(p.mode) if p is not None else None
    items = setup_pages(mode, bool(p is not None and p.icon_min_max),
                        bool(getattr(link, "bootloader", None) and link.bootloader != "B-EEV"))
    opts = [Option("Normal display", "leave the SETUP pages", lambda lk: setup_item(lk) == 0 and not lk.on_ld_page,
                   current=(now == 0), item=0)]
    order = [0] + items
    start = order.index(now) if now in order else None
    for item in items:
        label, detail = page_label(item)
        if item == 39:
            goal = (lambda lk: bool(lk.on_ld_page) or setup_item(lk) == 39)
        else:
            goal = (lambda lk, item=item: setup_item(lk) == item)
        blocked = ""
        if start is not None and item != now and not direct:
            k, path = start, []
            while order[k] != item:
                k = (k + 1) % len(order)
                path.append(order[k])
            if 39 in path[:-1]:
                blocked = ("after the page Ld, where the meter ignores keys from the app: press SETUP on the meter "
                           "to get past Ld, then pick it here")
        opts.append(Option(label, detail, goal, current=(now == item), blocked=blocked, item=item))
    if start is not None and now != 0 and 39 in order[start + 1:] and not direct:
        opts[0] = Option("Normal display", "leave the SETUP pages", opts[0].goal, current=False, item=0,
                         blocked="the way back passes the page Ld, where the meter ignores keys from the app: "
                                 "press SETUP on the meter")
    return opts


def supports_goto(link) -> bool:
    """Custom firmware C-1.08+ (CF-09) jumps to a SETUP page itself: key code 0xC0 + item, request SETPAGE on Ld."""
    v = link.firmware_version or ""
    if getattr(link, "is_demo", False) or not v.startswith("C-"):
        return False
    try:
        return (custom_key(v) or (0, 0)) >= (1, 8)
    except ValueError:
        return False


def goto_request(version: str | None) -> int:
    """Card-protocol request on the page Ld: SETPAGE item(1) (0x1C in C-1.08 / C-1.09, 0x1F since C-1.10)."""
    return setpage_type(version)


def goto_frame(item: int) -> bytes:
    """Key frame of CF-09: F4 + the code 0xC0 + item twice as two ASCII hex characters."""
    hx = f"{0xC0 + item:02X}".encode("ascii")
    return b"\xF4" + hx + hx


def can_navigate_setup(link) -> bool:
    """SETUP pages are known only from the custom firmware's page-text frame (C-1.03+)."""
    v = link.firmware_version or ""
    if getattr(link, "is_demo", False) or not v.startswith("C-"):
        return False
    try:
        return (custom_key(v) or (0, 0)) >= (1, 3)
    except ValueError:
        return False


def options_for(key: MeterKey, link) -> list[Option]:
    p = link.latest
    if key is MeterKey.MODE:
        return mode_options(p)
    if key is MeterKey.RANGE:
        return range_options(p)
    if key is MeterKey.HOLD:
        return hold_options(p)
    if key is MeterKey.MIN_MAX:
        return minmax_options(p)
    if key is MeterKey.SETUP and can_navigate_setup(link):
        return setup_options(link)
    return []


def max_presses(key: MeterKey, options: list[Option]) -> int:
    if key is MeterKey.SETUP:
        return 48                       # every page once, with the ones only some states show
    return len(options) + 2


# ---- the driver ----------------------------------------------------------------------------------------------------

def _observe(key: MeterKey, link):
    if key is MeterKey.SETUP:
        return setup_item(link), bool(link.on_ld_page)
    p = link.latest
    if p is None:
        return None
    if key is MeterKey.MODE:
        return _mode_state(p)
    if key is MeterKey.RANGE:
        return bool(p.icon_auto), p.main_range_index
    if key is MeterKey.HOLD:
        return _hold_state(p)
    if key is MeterKey.MIN_MAX:
        return p.icon2 & 0x07
    return None


class Navigator:
    """Press `key` until `option.goal(link)`; on_done(ok, text) at the end. stop() ends it early."""

    def __init__(self, link, key: MeterKey, option: Option, on_done: Callable[[bool, str], None],
                 limit: int | None = None, step_wait_s: float = STEP_WAIT_S, poll_ms: int = 100) -> None:
        self.link, self.key, self.option, self.on_done = link, key, option, on_done
        self.limit = limit if limit is not None else (1 if option.long_press else 48)
        self.step_wait_s = step_wait_s
        self.poll_ms = poll_ms
        self.presses = 0
        self.quiet = 0                  # presses in a row the meter did not react to
        self._timer = 0
        self._pressed_at = 0.0
        self._before = None
        self.running = False

    def start(self, direct: Callable[[], None] | None = None) -> None:
        """direct: send one request that does the whole way (CF-09) instead of key presses; then wait for the goal."""
        self.running = True
        if self.option.goal(self.link):
            self._finish(True, "already there")
            return
        if direct is not None:
            self._pressed_at = time.monotonic()
            direct()
            self._timer = GLib.timeout_add(self.poll_ms, self._poll_direct)
            return
        self._press()

    def _poll_direct(self) -> bool:
        if not self.running:
            return GLib.SOURCE_REMOVE
        if self.option.goal(self.link):
            self._timer = 0
            self._finish(True, "done")
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self._pressed_at > 2 * self.step_wait_s:
            self._timer = 0
            self._finish(False, "the meter did not go there (not available in this function?)")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def stop(self, text: str = "stopped") -> None:
        if self.running:
            self._finish(False, text)

    def _press(self) -> None:
        if not self.link.is_connected:
            self._finish(False, "not connected")
            return
        if self.presses >= self.limit:
            self._finish(False, f"not reached after {self.presses} presses (not available here?)")
            return
        self.presses += 1
        self._before = _observe(self.key, self.link)
        self._pressed_at = time.monotonic()
        self.link.send_key(self.key, self.option.long_press)
        self._timer = GLib.timeout_add(self.poll_ms, self._poll)

    def _poll(self) -> bool:
        if not self.running:
            return GLib.SOURCE_REMOVE
        now = _observe(self.key, self.link)
        changed = now != self._before          # any change shows the press took effect
        waited = time.monotonic() - self._pressed_at
        if not changed and waited < self.step_wait_s:
            return GLib.SOURCE_CONTINUE
        self._timer = 0
        if self.option.goal(self.link):
            self._finish(True, f"done in {self.presses} press{'es' if self.presses != 1 else ''}")
            return GLib.SOURCE_REMOVE
        if not changed:
            if self.key is MeterKey.SETUP and self._before == (21, False):
                # bt -> Ld: on the page Ld the meter stops sending frames; the link notices it a moment later
                if self._wants_ld():
                    self._finish(True, "the meter is on Ld")
                    return GLib.SOURCE_REMOVE
            else:
                self.quiet += 1
                if self.quiet >= 2:
                    self._finish(False, "the meter does not react")
                    return GLib.SOURCE_REMOVE
        else:
            self.quiet = 0
        if self.option.long_press:
            self._finish(False, "the meter did not change")
            return GLib.SOURCE_REMOVE
        self._press()
        return GLib.SOURCE_REMOVE

    def _wants_ld(self) -> bool:
        class _Ld:
            on_ld_page = True
            setup_text = None
            latest = None
        return bool(self.option.goal(_Ld()))

    def _finish(self, ok: bool, text: str) -> None:
        self.running = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.on_done(ok, text)


# ---- what each key has set now, for the line under its name ---------------------------------------------------------

def key_state(key: MeterKey, link) -> tuple[str, bool]:
    """(short text, the key's function is on) for the meter's present frame."""
    p = link.latest
    in_setup = bool(getattr(link, "in_setup_menu", False) or link.setup_text is not None)
    if key in (MeterKey.REL, MeterKey.PEAK) and in_setup:
        return key.setup_arrow or "", False
    if key is MeterKey.SETUP:
        if link.on_ld_page:
            return "Ld", True
        st = link.setup_text
        if st is not None:
            short = (PAGES.get(st.item) or PAGE_EXTRA.get(st.item) or ("",))[0]
            return short or f"page {st.item}", True
        return "", False
    if p is None:
        return "", False
    try:
        if key is MeterKey.MODE:
            return _mode_label(*_mode_state(p)), False
        if key is MeterKey.RANGE:
            if p.icon_auto:
                return "AUTO", False
            return range_label(int(p.mode), p.main_range_index), True
        if key is MeterKey.HOLD:
            return {2: ("HOLD", True), 1: ("A-HOLD", True)}.get(_hold_state(p), ("", False))
        if key is MeterKey.REL:
            return ("on", True) if p.icon_rel else ("", False)
        if key is MeterKey.MIN_MAX:
            label = p.min_max_label
            return (label.replace("MAX MIN AVG", "all"), True) if label else ("", False)
        if key is MeterKey.MEM:
            return ("on", True) if p.icon_mem else ("", False)
    except (ValueError, KeyError, IndexError):
        pass
    return "", False
