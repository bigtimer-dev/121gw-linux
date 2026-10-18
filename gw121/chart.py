"""Trend chart: auto-scaled to the meter range, with zoom / pan / hover readout.

Drawing cost is independent of log length: a min/max pyramid (buckets of 4, 16, … 4096
samples) is updated incrementally as samples arrive, and each redraw reads at most
~4 buckets per pixel column.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cairo
from gi.repository import Adw, Gtk

from .sessions import DataPoint

MIN_SPAN = 0.02
TEMP_RGB = (1.0, 0.47, 0.0)
FAN = 4
LEVELS = 6                      # bucket sizes 4 … 4096
INF = math.inf
NAN = math.nan
_isfinite = math.isfinite


@dataclass
class Palette:
    fg: tuple[float, float, float]
    muted: tuple[float, float, float, float]
    grid: tuple[float, float, float, float]
    accent: tuple[float, float, float]
    bg: tuple[float, float, float] | None = None


def fmt_num(v: float) -> str:
    if not math.isfinite(v):
        return "—"
    a = abs(v)
    if a != 0 and (a >= 100_000 or a < 0.001):
        return f"{v:.4g}"
    return f"{v:.5g}"


def fmt_elapsed(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{int(sec // 60)}:{int(sec % 60):02d}"
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{int(sec % 60):02d}"


def visible_range(n: int, span: float, start: float) -> tuple[int, int]:
    span = min(1.0, max(MIN_SPAN, span))
    start = min(max(0.0, start), max(0.0, 1.0 - span))
    last = n - 1
    i0 = min(last, max(0, math.floor(start * last)))
    i1 = min(last, max(i0, math.ceil((start + span) * last)))
    return i0, i1


class Summary:
    """Incremental per-sample arrays plus a min/max pyramid over a list of DataPoints."""

    def __init__(self) -> None:
        self._reset(None)

    def _reset(self, src) -> None:
        self.src = src
        self.first = None
        self.vals: list[float] = []      # plot value (NaN when not finite)
        self.temps: list[float] = []     # temperature (NaN when absent)
        self.fs: list[float] = []        # full scale (−inf when unknown)
        self.lb: list[float] = []        # lower bound (+inf when unknown)
        # per level: [lo, hi, temp_sum, temp_count, fs_max, lb_min]
        self.levels: list[list[list[float]]] = [[[], [], [], [], [], []] for _ in range(LEVELS)]

    def __len__(self) -> int:
        return len(self.vals)

    def sync(self, points: list[DataPoint]) -> None:
        """Bring the summary up to date. Appends are incremental; anything else rebuilds."""
        incremental = (points is self.src and len(points) >= len(self.vals)
                       and (not self.vals or points[0] is self.first))
        if not incremental:
            self._reset(points)
        self.first = points[0] if points else None
        for i in range(len(self.vals), len(points)):
            self._add(i, points[i])

    def _add(self, i: int, p: DataPoint) -> None:
        v = p.plot_value if _isfinite(p.plot_value) else NAN
        t = p.temperature_value
        t = t if t is not None and _isfinite(t) else NAN
        fs = p.plot_full_scale if _isfinite(p.plot_full_scale) and p.plot_full_scale > 0 else -INF
        lb = p.plot_lower_bound if _isfinite(p.plot_lower_bound) else INF
        self.vals.append(v)
        self.temps.append(t)
        self.fs.append(fs)
        self.lb.append(lb)
        size = 1
        for lo, hi, ts, tc, fsm, lbm in self.levels:
            size *= FAN
            j = i // size
            if j == len(lo):
                lo.append(INF), hi.append(-INF), ts.append(0.0), tc.append(0)
                fsm.append(-INF), lbm.append(INF)
            if v == v:  # not NaN
                if v < lo[j]:
                    lo[j] = v
                if v > hi[j]:
                    hi[j] = v
            if t == t:
                ts[j] += t
                tc[j] += 1
            if fs > fsm[j]:
                fsm[j] = fs
            if lb < lbm[j]:
                lbm[j] = lb


@dataclass
class _Columns:
    """Data-space series for one redraw (x as fractional sample position 0..count-1)."""
    xs: list[float]
    lo: list[float]
    hi: list[float]
    temp: list[tuple[float, float]]
    fs_max: float
    lb_min: float


def _columns(s: Summary, a: int, b: int, cols: int) -> _Columns:
    count = b - a + 1
    per = count / max(1, cols)
    if per <= FAN:
        vals = s.vals[a : b + 1]
        temps = s.temps[a : b + 1]
        xs = [float(i) for i in range(count)]
        return _Columns(xs, vals, vals, [(float(i), t) for i, t in enumerate(temps) if t == t],
                        max(s.fs[a : b + 1]), min(s.lb[a : b + 1]))

    k = 0
    size = FAN
    while k + 1 < LEVELS and size * FAN <= per:
        size *= FAN
        k += 1
    lo_l, hi_l, ts_l, tc_l, fs_l, lb_l = s.levels[k]
    clo = [INF] * cols
    chi = [-INF] * cols
    cts = [0.0] * cols
    ctc = [0] * cols
    fs_max, lb_min = -INF, INF
    scale = cols / count
    for j in range(a // size, b // size + 1):
        c = int((j * size + size / 2 - a) * scale)
        c = 0 if c < 0 else cols - 1 if c >= cols else c
        lo, hi = lo_l[j], hi_l[j]
        if lo < clo[c]:
            clo[c] = lo
        if hi > chi[c]:
            chi[c] = hi
        cts[c] += ts_l[j]
        ctc[c] += tc_l[j]
        if fs_l[j] > fs_max:
            fs_max = fs_l[j]
        if lb_l[j] < lb_min:
            lb_min = lb_l[j]
    step = (count - 1) / max(1, cols - 1)
    xs, los, his, temp = [], [], [], []
    for c in range(cols):
        x = c * step
        if clo[c] <= chi[c]:
            xs.append(x)
            los.append(clo[c])
            his.append(chi[c])
        if ctc[c]:
            temp.append((x, cts[c] / ctc[c]))
    return _Columns(xs, los, his, temp, fs_max, lb_min)


def _bounds(cols: _Columns, autoscale: bool) -> tuple[float, float]:
    finite_lo = [v for v in cols.lo if v == v]
    finite_hi = [v for v in cols.hi if v == v]
    dmin = min(finite_lo) if finite_lo else None
    dmax = max(finite_hi) if finite_hi else None
    if not autoscale and cols.fs_max > 0 and _isfinite(cols.fs_max) and _isfinite(cols.lb_min):
        lo, hi = cols.lb_min, cols.fs_max
    elif dmin is not None:
        pad = max(abs(dmin) * 0.05, 1e-9) if dmin == dmax else (dmax - dmin) * (0.08 if autoscale else 0.05)
        lo, hi = dmin - pad, dmax + pad
    else:
        lo, hi = 0.0, 1.0
    if not (_isfinite(lo) and _isfinite(hi)) or hi <= lo:
        lo = dmin if dmin is not None else 0.0
        hi = max(lo + 1, dmax if dmax is not None else 1.0)
    return lo, hi


def draw_chart(cr: cairo.Context, w: float, h: float, points: list[DataPoint], unit: str, *,
               show_temperature: bool, span: float, start: float, pal: Palette,
               autoscale: bool = False, title: str | None = None, font_scale: float = 1.0,
               hover_x: float | None = None, empty_text: str = "No samples yet",
               summary: Summary | None = None, window: int | None = None) -> None:
    """Draw the trend. `window` limits the view to the newest N samples of `points`."""
    if summary is None:
        summary = Summary()
        summary.sync(points)
    if pal.bg:
        cr.set_source_rgb(*pal.bg)
        cr.paint()
    cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    fs = 10 * font_scale

    def text(s, x, y, rgba, size=fs, anchor="left"):
        cr.set_font_size(size)
        cr.set_source_rgba(*rgba) if len(rgba) == 4 else cr.set_source_rgb(*rgba)
        ext = cr.text_extents(s)
        if anchor == "center":
            x -= ext.x_advance / 2
        elif anchor == "right":
            x -= ext.x_advance
        cr.move_to(x, y)
        cr.show_text(s)
        return ext.x_advance

    top = 22 * font_scale if title else 4
    if title:
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        text(title, 8, 16 * font_scale, pal.fg, 13 * font_scale)
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

    total = len(points)
    base = max(0, total - window) if window else 0
    n = total - base
    if n <= 0:
        text(empty_text, w / 2, h / 2, pal.muted, 12 * font_scale, "center")
        return

    i0, i1 = visible_range(n, span, start)
    a, b = base + i0, base + i1
    count = b - a + 1
    y_unit = points[b].plot_unit or unit

    pad_r = 6
    pad_b = 16 * font_scale
    # first pass with an estimated width; label width barely changes the column count
    est_cols = max(1, int(w - 70 - pad_r))
    data = _columns(summary, a, b, est_cols)
    lo, hi = _bounds(data, autoscale)

    cr.set_font_size(fs)
    label_w = max(cr.text_extents(fmt_num(v)).x_advance for v in (lo, hi, (lo + hi) / 2))
    pad_l = max(label_w, cr.text_extents(y_unit).x_advance) + 10
    pw = max(1.0, w - pad_l - pad_r)
    ph = max(1.0, h - pad_b - top)
    if int(pw) != est_cols and count / max(1, int(pw)) > FAN:
        data = _columns(summary, a, b, int(pw))

    yr = hi - lo
    y0 = top + ph - ph * 0.04
    yk = ph * 0.92 / yr

    def ymap(v):
        t = (v - lo) / yr
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        return top + ph - t * ph * 0.92 - ph * 0.04

    # grid + y labels
    cr.set_line_width(1)
    cr.set_source_rgba(*pal.grid)
    for k in range(5):
        y = round(top + ph * k / 4) + 0.5
        cr.move_to(pad_l, y)
        cr.line_to(w - pad_r, y)
    cr.stroke()
    for frac in (0.0, 0.5, 1.0):
        v = lo + yr * frac
        text(fmt_num(v), pad_l - 5, ymap(v) + fs * 0.35, pal.muted, anchor="right")
    text(y_unit, 2, top + fs, pal.fg, fs * 1.05)
    if lo < 0 < hi:
        cr.set_source_rgba(*pal.grid[:3], min(1, pal.grid[3] * 2.5))
        cr.move_to(pad_l, ymap(0))
        cr.line_to(w - pad_r, ymap(0))
        cr.stroke()

    xk = pw / (count - 1) if count > 1 else 0.0
    t0 = points[0].timestamp

    def xmap(i):
        return pad_l + (i * xk if count > 1 else pw / 2)

    for k in range(5):
        i = min(count - 1, round((count - 1) * k / 4))
        anchor = "left" if k == 0 else "right" if k == 4 else "center"
        text(fmt_elapsed(points[a + i].timestamp - t0), xmap(i), h - 3, pal.muted, anchor=anchor)

    if count > 1 and data.xs:
        def clampy(v):
            t = (v - lo) / yr
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            return y0 - t * ph * 0.92

        bottom_v = lo
        pts: list[tuple[float, float]] = []
        if data.lo is data.hi:  # raw samples
            for x, v in zip(data.xs, data.lo):
                pts.append((pad_l + x * xk, clampy(v if v == v else bottom_v)))
        else:
            for x, l, hv in zip(data.xs, data.lo, data.hi):
                px = pad_l + x * xk
                pts.append((px, clampy(l)))
                pts.append((px, clampy(hv)))

        base_y = ymap(0.0) if lo < 0 < hi else top + ph
        cr.move_to(pts[0][0], base_y)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], base_y)
        cr.close_path()
        cr.set_source_rgba(*pal.accent, 0.14)
        cr.fill()

        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(2 * font_scale)
        cr.move_to(*pts[0])
        for p in pts[1:]:
            cr.line_to(*p)
        cr.set_source_rgb(*pal.accent)
        cr.stroke()

        if show_temperature and len(data.temp) > 1:
            t_unit = next((points[i].temperature_unit for i in (b, a) if points[i].temperature_unit), "°C")
            tvals = [t for _, t in data.temp]
            tlo = min(0 if t_unit == "°F" else -20, min(tvals))
            thi = max(212 if t_unit == "°F" else 100, max(tvals))
            tr = max(1e-9, thi - tlo)
            cr.set_line_width(1.5 * font_scale)
            cr.set_dash([5 * font_scale, 3 * font_scale])
            x, tv = data.temp[0]
            cr.move_to(pad_l + x * xk, y0 - (tv - tlo) / tr * ph * 0.92)
            for x, tv in data.temp[1:]:
                cr.line_to(pad_l + x * xk, y0 - (tv - tlo) / tr * ph * 0.92)
            cr.set_source_rgb(*TEMP_RGB)
            cr.stroke()
            cr.set_dash([])
            last_t = summary.temps[b] if summary.temps[b] == summary.temps[b] else tvals[-1]
            text(f"{last_t:.1f} {t_unit}", w - pad_r, top + fs, TEMP_RGB, anchor="right")

    # hover crosshair
    if hover_x is not None and pad_l <= hover_x <= w - pad_r:
        i = min(count - 1, max(0, round((hover_x - pad_l) / pw * (count - 1)))) if count > 1 else 0
        p = points[a + i]
        x = xmap(i)
        cr.set_source_rgba(*pal.fg, 0.45)
        cr.set_line_width(1)
        cr.move_to(round(x) + 0.5, top)
        cr.line_to(round(x) + 0.5, top + ph)
        cr.stroke()
        if _isfinite(p.plot_value):
            cr.arc(x, ymap(p.plot_value), 3.5, 0, 2 * math.pi)
            cr.set_source_rgb(*pal.accent)
            cr.fill()
        label = f"{fmt_num(p.plot_value)} {p.plot_unit} @ {fmt_elapsed(p.timestamp - t0)}"
        cr.set_font_size(fs)
        tw = cr.text_extents(label).x_advance
        lx = min(max(pad_l + 2, x - tw / 2), w - pad_r - tw - 2)
        cr.set_source_rgba(*(pal.bg or (0.5, 0.5, 0.5)), 0.85)
        cr.rectangle(lx - 3, top + 2, tw + 6, fs + 5)
        cr.fill()
        text(label, lx, top + fs + 3, pal.fg)


def _rgb(c) -> tuple[float, float, float]:
    return (c.red, c.green, c.blue)


class TrendChart(Gtk.DrawingArea):
    def __init__(self, height: int = 200):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.points: list[DataPoint] = []
        self.summary = Summary()
        self.window: int | None = None
        self.unit = ""
        self.show_temperature = True
        self.autoscale = False
        self.empty_text = "No samples yet"
        self.span = 1.0
        self.start = 0.0
        self.follow = True          # keep the newest sample in view while zoomed
        self._hover_x: float | None = None
        self._drag_start = 0.0
        self.set_draw_func(self._draw)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda *_: setattr(self, "_drag_start", self.start))
        drag.connect("drag-update", self._on_drag)
        self.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_click)
        self.add_controller(click)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, _y: self._set_hover(x))
        motion.connect("leave", lambda _c: self._set_hover(None))
        self.add_controller(motion)

    def set_data(self, points: list[DataPoint], unit: str = "", window: int | None = None) -> None:
        """Show `points` (newest `window` samples if given). Appending to the same list is O(new samples)."""
        self.points = points
        self.window = window
        self.unit = unit
        self.summary.sync(points)
        if self.follow and self.span < 1:
            self.start = 1 - self.span
        self.queue_draw()

    def reset_view(self) -> None:
        self.span, self.start, self.follow = 1.0, 0.0, True
        self.queue_draw()

    def _visible_count(self) -> int:
        n = len(self.points)
        return min(n, self.window) if self.window else n

    def _set_hover(self, x):
        self._hover_x = x
        self.queue_draw()

    def _on_scroll(self, _c, _dx, dy) -> bool:
        if self._visible_count() < 2:
            return False
        old = self.span
        new = min(1.0, max(MIN_SPAN, old * (1.18 if dy > 0 else 0.85)))
        center = self.start + old / 2
        if self._hover_x is not None and self.get_width() > 0:
            center = self.start + old * min(1, max(0, self._hover_x / self.get_width()))
        self.span = new
        self.start = min(max(0.0, center - new / 2), max(0.0, 1 - new))
        self.follow = self.start + self.span >= 0.999
        self.queue_draw()
        return True

    def _on_drag(self, _g, dx, _dy) -> None:
        delta = dx / max(1, self.get_width()) * self.span
        self.start = min(max(0.0, self._drag_start - delta), max(0.0, 1 - self.span))
        self.follow = self.start + self.span >= 0.999
        self.queue_draw()

    def _on_click(self, _g, n_press, _x, _y) -> None:
        if n_press == 2:
            self.reset_view()

    def palette(self) -> Palette:
        fg = _rgb(self.get_color())
        try:
            accent = _rgb(Adw.StyleManager.get_default().get_accent_color_rgba())
        except AttributeError:
            accent = (0.21, 0.52, 0.89)
        return Palette(fg=fg, muted=(*fg, 0.6), grid=(*fg, 0.12), accent=accent)

    def _draw(self, _area, cr, w, h) -> None:
        draw_chart(cr, w, h, self.points, self.unit, show_temperature=self.show_temperature,
                   span=self.span, start=self.start, pal=self.palette(), autoscale=self.autoscale,
                   hover_x=self._hover_x, empty_text=self.empty_text,
                   summary=self.summary, window=self.window)


def export_png(path: str, points: list[DataPoint], unit: str, title: str, show_temperature: bool,
               width: int = 1400, height: int = 800) -> None:
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    cr = cairo.Context(surf)
    pal = Palette(fg=(0.1, 0.1, 0.1), muted=(0.35, 0.35, 0.38, 1), grid=(0, 0, 0, 0.12),
                  accent=(0.21, 0.52, 0.89), bg=(1, 1, 1))
    draw_chart(cr, width, height, points, unit, show_temperature=show_temperature,
               span=1.0, start=0.0, pal=pal, title=title, font_scale=1.6)
    surf.write_to_png(path)
