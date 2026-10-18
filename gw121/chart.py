"""Trend chart: auto-scaled to the meter range, with zoom / pan / hover readout."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cairo
from gi.repository import Adw, Gtk

from .sessions import DataPoint

MIN_SPAN = 0.02
TEMP_RGB = (1.0, 0.47, 0.0)


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


def y_bounds(points: list[DataPoint]) -> tuple[float, float]:
    finite = [p.plot_value for p in points if math.isfinite(p.plot_value)]
    scales = [p.plot_full_scale for p in points if math.isfinite(p.plot_full_scale) and p.plot_full_scale > 0]
    lows = [p.plot_lower_bound for p in points if math.isfinite(p.plot_lower_bound)]
    if scales and lows:
        lo, hi = min(lows), max(scales)
    elif finite:
        dmin, dmax = min(finite), max(finite)
        pad = max(abs(dmin) * 0.05, 1e-9) if dmin == dmax else (dmax - dmin) * 0.05
        lo, hi = dmin - pad, dmax + pad
    else:
        lo, hi = 0.0, 1.0
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        lo = min(finite) if finite else 0.0
        hi = max(lo + 1, max(finite) if finite else 1.0)
    return lo, hi


def draw_chart(cr: cairo.Context, w: float, h: float, points: list[DataPoint], unit: str, *,
               show_temperature: bool, span: float, start: float, pal: Palette,
               autoscale: bool = False, title: str | None = None, font_scale: float = 1.0,
               hover_x: float | None = None, empty_text: str = "No samples yet") -> None:
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

    if not points:
        text(empty_text, w / 2, h / 2, pal.muted, 12 * font_scale, "center")
        return

    i0, i1 = visible_range(len(points), span, start)
    sl = points[i0 : i1 + 1]
    y_unit = next((p.plot_unit for p in reversed(sl) if p.plot_unit), unit)
    if autoscale:
        finite = [p.plot_value for p in sl if math.isfinite(p.plot_value)]
        if finite:
            dmin, dmax = min(finite), max(finite)
            pad = max(abs(dmin) * 0.05, 1e-9) if dmin == dmax else (dmax - dmin) * 0.08
            lo, hi = dmin - pad, dmax + pad
        else:
            lo, hi = 0.0, 1.0
    else:
        lo, hi = y_bounds(sl)

    cr.set_font_size(fs)
    label_w = max(cr.text_extents(fmt_num(v)).x_advance for v in (lo, hi, (lo + hi) / 2))
    pad_l = max(label_w, cr.text_extents(y_unit).x_advance) + 10
    pad_b = 16 * font_scale
    pad_r = 6
    pw = max(1.0, w - pad_l - pad_r)
    ph = max(1.0, h - pad_b - top)

    def ymap(v):
        t = min(1.0, max(0.0, (v - lo) / (hi - lo)))
        return top + ph - t * ph * 0.92 - ph * 0.04

    # grid + y labels
    cr.set_line_width(1)
    for k in range(5):
        y = top + ph * k / 4
        cr.set_source_rgba(*pal.grid)
        cr.move_to(pad_l, round(y) + 0.5)
        cr.line_to(w - pad_r, round(y) + 0.5)
        cr.stroke()
    for frac in (0.0, 0.5, 1.0):
        v = lo + (hi - lo) * frac
        text(fmt_num(v), pad_l - 5, ymap(v) + fs * 0.35, pal.muted, anchor="right")
    text(y_unit, 2, top + fs, pal.fg, fs * 1.05)
    if lo < 0 < hi:
        cr.set_source_rgba(*pal.grid[:3], min(1, pal.grid[3] * 2.5))
        cr.move_to(pad_l, ymap(0))
        cr.line_to(w - pad_r, ymap(0))
        cr.stroke()

    n = len(sl)
    t0 = points[0].timestamp

    def xmap(i):
        return pad_l + (i / (n - 1) if n > 1 else 0.5) * pw

    # x labels (elapsed time since log start)
    for k in range(5):
        i = min(n - 1, round((n - 1) * k / 4))
        anchor = "left" if k == 0 else "right" if k == 4 else "center"
        text(fmt_elapsed(sl[i].timestamp - t0), xmap(i), h - 3, pal.muted, anchor=anchor)

    if n > 1:
        # decimate to ~2 points per pixel column (min/max) for long logs
        cols = max(1, int(pw))
        if n > cols * 4:
            xs, ys = [], []
            per = n / cols
            for c in range(cols):
                a, b = int(c * per), min(n, int((c + 1) * per) + 1)
                vals = [p.plot_value for p in sl[a:b] if math.isfinite(p.plot_value)] or [lo]
                x = pad_l + c / max(1, cols - 1) * pw
                xs += [x, x]
                ys += [ymap(min(vals)), ymap(max(vals))]
            pts = list(zip(xs, ys))
        else:
            pts = [(xmap(i), ymap(p.plot_value if math.isfinite(p.plot_value) else lo)) for i, p in enumerate(sl)]

        base = ymap(0.0) if lo < 0 < hi else top + ph
        cr.move_to(pts[0][0], base)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], base)
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

        if show_temperature:
            temps = [(i, p.temperature_value) for i, p in enumerate(sl)
                     if p.temperature_value is not None and math.isfinite(p.temperature_value)]
            if len(temps) > 1:
                t_unit = next((p.temperature_unit for p in sl if p.temperature_unit), "°C")
                vals = [t for _, t in temps]
                tlo = min(0 if t_unit == "°F" else -20, min(vals))
                thi = max(212 if t_unit == "°F" else 100, max(vals))
                step = max(1, len(temps) // (cols * 2) if n > cols * 4 else 1)
                cr.set_line_width(1.5 * font_scale)
                cr.set_dash([5 * font_scale, 3 * font_scale])
                first = True
                for i, tv in temps[::step]:
                    norm = min(1.0, max(0.0, (tv - tlo) / max(1e-9, thi - tlo)))
                    x, y = xmap(i), top + ph - norm * ph * 0.92 - ph * 0.04
                    cr.move_to(x, y) if first else cr.line_to(x, y)
                    first = False
                cr.set_source_rgb(*TEMP_RGB)
                cr.stroke()
                cr.set_dash([])
                text(f"{vals[-1]:.1f} {t_unit}", w - pad_r, top + fs, TEMP_RGB, anchor="right")

    # hover crosshair
    if hover_x is not None and pad_l <= hover_x <= w - pad_r:
        i = min(n - 1, max(0, round((hover_x - pad_l) / pw * (n - 1)))) if n > 1 else 0
        p = sl[i]
        x = xmap(i)
        cr.set_source_rgba(*pal.fg, 0.45)
        cr.set_line_width(1)
        cr.move_to(round(x) + 0.5, top)
        cr.line_to(round(x) + 0.5, top + ph)
        cr.stroke()
        if math.isfinite(p.plot_value):
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

    def set_data(self, points: list[DataPoint], unit: str = "") -> None:
        self.points = points
        self.unit = unit
        if self.follow and self.span < 1:
            self.start = 1 - self.span
        self.queue_draw()

    def reset_view(self) -> None:
        self.span, self.start, self.follow = 1.0, 0.0, True
        self.queue_draw()

    def _set_hover(self, x):
        self._hover_x = x
        self.queue_draw()

    def _on_scroll(self, _c, _dx, dy) -> bool:
        if len(self.points) < 2:
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
            acc = Adw.StyleManager.get_default().get_accent_color_rgba()
            accent = _rgb(acc)
        except AttributeError:
            accent = (0.21, 0.52, 0.89)
        return Palette(fg=fg, muted=(*fg, 0.6), grid=(*fg, 0.12), accent=accent)

    def _draw(self, _area, cr, w, h) -> None:
        draw_chart(cr, w, h, self.points, self.unit, show_temperature=self.show_temperature,
                   span=self.span, start=self.start, pal=self.palette(), autoscale=self.autoscale,
                   hover_x=self._hover_x, empty_text=self.empty_text)


def export_png(path: str, points: list[DataPoint], unit: str, title: str, show_temperature: bool,
               width: int = 1400, height: int = 800) -> None:
    surf = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    cr = cairo.Context(surf)
    pal = Palette(fg=(0.1, 0.1, 0.1), muted=(0.35, 0.35, 0.38, 1), grid=(0, 0, 0, 0.12),
                  accent=(0.21, 0.52, 0.89), bg=(1, 1, 1))
    draw_chart(cr, width, height, points, unit, show_temperature=show_temperature,
               span=1.0, start=0.0, pal=pal, title=title, font_scale=1.6)
    surf.write_to_png(path)
