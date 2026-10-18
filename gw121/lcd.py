"""LCD replica of the 121GW display."""

from __future__ import annotations

from gi.repository import Gtk

from .packet import Coupling, PacketV2

INK = (0x1F / 255, 0x24 / 255, 0x1F / 255)

CSS = """
.lcd {
  background-color: #b8c7b2;
  color: #1f241f;
  border-radius: 14px;
  border: 1px solid alpha(black, 0.25);
  padding: 12px 14px;
  box-shadow: inset 0 2px 6px alpha(black, 0.18);
}
.lcd.dimmed { background-color: #a9b5a4; }
.lcd .ann { font-family: monospace; font-weight: 800; font-size: 9pt; color: alpha(#1f241f, 0.16); }
.lcd .ann.on { color: #1f241f; }
.lcd .digits { font-family: "DSEG7 Classic", "DejaVu Sans Mono", monospace; font-size: 44pt; font-weight: 700;
               letter-spacing: 1px; }
.lcd .sign { font-family: monospace; font-size: 34pt; font-weight: 700; }
.lcd .sign.off { color: alpha(#1f241f, 0.12); }
.lcd .unit { font-family: monospace; font-size: 22pt; font-weight: 800; }
.lcd .mode-name { font-family: monospace; font-size: 9pt; font-weight: 700; color: alpha(#1f241f, 0.6); }
.lcd .tag { font-family: monospace; font-weight: 800; font-size: 8.5pt; padding: 1px 5px; border-radius: 3px;
            color: alpha(#1f241f, 0.16); }
.lcd .tag.on { background-color: #1f241f; color: #b8c7b2; }
.lcd .subtitle { font-family: monospace; font-size: 9pt; font-weight: 800; color: alpha(#1f241f, 0.7); }
.lcd .subreading { font-family: "DSEG7 Classic", "DejaVu Sans Mono", monospace; font-size: 18pt; font-weight: 700; }
.lcd .lcdstatus { font-family: monospace; font-size: 8pt; color: alpha(#1f241f, 0.55); }
.lcd separator { background-color: alpha(#1f241f, 0.25); }
.meter-key { font-weight: 700; min-height: 44px; padding: 2px 4px; }
.meter-key.pressed { background-color: alpha(@accent_bg_color, 0.35); }
.meter-key .arrow { font-size: 8pt; opacity: 0.75; }
.statusbar-label { font-family: monospace; font-size: 9pt; font-weight: 700; }
.card-title { font-size: 10pt; opacity: 0.7; }
.mono { font-family: monospace; }
.stat-tile { padding: 8px 10px; border-radius: 8px; background-color: alpha(currentColor, 0.06); }
.stat-title { font-size: 9pt; opacity: 0.65; }
.stat-value { font-family: monospace; font-weight: 600; }
.temp-toggle { color: #ff7800; }
.temp-toggle.off { color: alpha(currentColor, 0.5); }
"""


def _ann(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("ann")
    return lbl


def _set_on(widget: Gtk.Widget, on: bool) -> None:
    if on:
        widget.add_css_class("on")
    else:
        widget.remove_css_class("on")


class BarGraph(Gtk.DrawingArea):
    """26-segment analog bar with 0-5 (or 0-10 for the 150 % scale) tick labels."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_height(30)
        self.set_hexpand(True)
        self.segments = 0
        self.used = False
        self.wide_scale = False
        self.negative = False
        self.scale_label = ""
        self.set_draw_func(self._draw)

    def update(self, p: PacketV2 | None) -> None:
        if p is None:
            self.used, self.segments = False, 0
        else:
            self.used = p.bar_is_used
            self.segments = p.bar_segments
            self.wide_scale = p.bar_uses_150_scale
            self.negative = p.bar_is_negative
            self.scale_label = f"{p.bar_nominal_scale:g}"
        self.queue_draw()

    def _draw(self, _a, cr, w, h) -> None:
        dim = 0.13
        sign_w = 14
        x0, x1 = sign_w, w - 34
        seg_w = (x1 - x0) / 26
        cr.select_font_face("monospace", 0, 1)
        cr.set_font_size(9)
        # sign
        cr.set_source_rgba(*INK, 1 if self.used and self.negative else dim)
        cr.rectangle(2, 7, 8, 2.4)
        cr.fill()
        # segments
        for i in range(26):
            on = self.used and i < self.segments
            cr.set_source_rgba(*INK, 1 if on else dim)
            cr.rectangle(x0 + i * seg_w + 0.8, 4, seg_w - 1.6, 9 if i % 5 else 12)
            cr.fill()
        # ticks
        labels = ["0", "2", "4", "6", "8", "10"] if self.wide_scale else ["0", "1", "2", "3", "4", "5"]
        cr.set_source_rgba(*INK, 0.85 if self.used else dim)
        for k, t in enumerate(labels):
            x = x0 + k * 5 * seg_w
            ext = cr.text_extents(t)
            cr.move_to(x - ext.x_advance / 2, h - 3)
            cr.show_text(t)
        if self.used and self.scale_label:
            cr.move_to(x1 + 6, 13)
            cr.show_text(self.scale_label)


class LcdView(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("lcd")

        top = Gtk.Box(spacing=8)
        self.i_bt, self.i_auto, self.i_1khz, self.i_1ms, self.i_lowz = (
            _ann(t) for t in ("BT", "AUTO", "1kHz", "1ms", "LowZ"))
        for w in (self.i_bt, self.i_auto, self.i_1khz, self.i_1ms, self.i_lowz):
            top.append(w)
        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)
        self.i_bat, self.i_apo = _ann("BAT"), _ann("APO")
        top.append(self.i_bat)
        top.append(self.i_apo)
        self.append(top)

        self.mode_name = Gtk.Label(xalign=0)
        self.mode_name.add_css_class("mode-name")

        main = Gtk.Box(spacing=0)
        self.sign = Gtk.Label(label="-")
        self.sign.add_css_class("sign")
        self.sign.set_valign(Gtk.Align.END)
        self.digits = Gtk.Label(label="---.---")
        self.digits.add_css_class("digits")
        self.digits.set_hexpand(True)
        self.digits.set_xalign(1)
        self.unit = Gtk.Label(label="", xalign=0)
        self.unit.add_css_class("unit")
        self.unit.set_valign(Gtk.Align.END)
        self.unit.set_margin_start(8)
        self.unit.set_margin_bottom(8)
        self.unit.set_size_request(70, -1)
        main.append(self.sign)
        main.append(self.digits)
        main.append(self.unit)
        self.append(main)

        tags = Gtk.Box(spacing=6)
        self.t_dc, self.t_ac, self.t_acdc, self.t_diode, self.t_cont = (
            Gtk.Label(label=t) for t in ("DC", "AC", "DC+AC", "DIODE", "CONT"))
        for i, t in enumerate((self.t_dc, self.t_ac, self.t_acdc, self.t_diode, self.t_cont)):
            t.add_css_class("tag")
            if i == 3:
                t.set_margin_start(10)
            tags.append(t)
        tags.append(Gtk.Box(hexpand=True))
        tags.append(self.mode_name)
        self.append(tags)

        self.bar = BarGraph()
        self.bar.set_margin_top(4)
        self.append(self.bar)

        sep = Gtk.Separator()
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        self.append(sep)

        sub = Gtk.Box(spacing=8)
        self.sub_title = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.sub_title.add_css_class("subtitle")
        self.sub_reading = Gtk.Label(xalign=1)
        self.sub_reading.add_css_class("subreading")
        sub.append(self.sub_title)
        sub.append(self.sub_reading)
        self.append(sub)

        bottom = Gtk.Box(spacing=8)
        bottom.set_margin_top(6)
        self.i_rel, self.i_hold, self.i_minmax, self.i_mem, self.i_dbm, self.i_test = (
            _ann(t) for t in ("REL", "HOLD", "MIN/MAX", "MEM", "dBm", "TEST"))
        for w in (self.i_rel, self.i_hold, self.i_minmax, self.i_mem, self.i_dbm, self.i_test):
            bottom.append(w)
        bottom.append(Gtk.Box(hexpand=True))
        self.status = Gtk.Label(xalign=1, ellipsize=3)
        self.status.add_css_class("lcdstatus")
        bottom.append(self.status)
        self.append(bottom)

    def clear(self, status: str = "") -> None:
        for w in (self.i_bt, self.i_auto, self.i_1khz, self.i_1ms, self.i_lowz, self.i_bat, self.i_apo,
                  self.i_rel, self.i_hold, self.i_minmax, self.i_mem, self.i_dbm, self.i_test,
                  self.t_dc, self.t_ac, self.t_acdc, self.t_diode, self.t_cont):
            _set_on(w, False)
        self.i_minmax.set_label("MIN/MAX")
        self.i_hold.set_label("HOLD")
        self.sign.add_css_class("off")
        self.digits.set_label("---.---")
        self.unit.set_label("")
        self.mode_name.set_label("")
        self.sub_title.set_label("")
        self.sub_reading.set_label("")
        self.bar.update(None)
        self.status.set_label(status)
        self.add_css_class("dimmed")

    def update(self, p: PacketV2, *, in_setup_menu: bool, status: str) -> None:
        self.remove_css_class("dimmed")
        _set_on(self.i_bt, p.icon_bt)
        _set_on(self.i_auto, p.icon_auto)
        _set_on(self.i_1khz, p.icon_1khz)
        _set_on(self.i_1ms, p.icon_1ms)
        _set_on(self.i_lowz, p.icon_low_z)
        _set_on(self.i_bat, p.icon_bat)
        _set_on(self.i_apo, p.icon_apo)
        _set_on(self.i_rel, p.icon_rel)
        _set_on(self.i_hold, p.icon_hold)
        self.i_hold.set_label("A-HOLD" if p.icon_auto_hold else "HOLD")
        _set_on(self.i_minmax, p.icon_min_max)
        self.i_minmax.set_label(p.min_max_label or "MIN/MAX")
        _set_on(self.i_mem, p.icon_mem)
        _set_on(self.i_dbm, p.icon_dbm)
        _set_on(self.i_test, p.icon_test)

        if p.is_main_negative and not p.is_main_overflow:
            self.sign.remove_css_class("off")
        else:
            self.sign.add_css_class("off")
        self.digits.set_label("  OL  " if p.is_main_overflow else p.formatted_main_value)
        self.unit.set_label(p.main_unit)
        self.mode_name.set_label(p.mode_name.upper())

        c = p.coupling
        _set_on(self.t_dc, c == Coupling.DC)
        _set_on(self.t_ac, c == Coupling.AC)
        _set_on(self.t_acdc, c == Coupling.ACDC)
        _set_on(self.t_diode, p.is_diode_mode)
        _set_on(self.t_cont, p.icon_cont)
        self.bar.update(p)

        desc = p.setup_descriptor() if in_setup_menu else None
        if desc is not None:
            self.sub_title.set_label("SETUP · " + desc.title)
            self.sub_reading.set_label(desc.reading)
        else:
            pres = p.setup_sub_presentation()
            self.sub_title.set_label("Calibration" if p.icon_test else (pres.label if pres else p.sub_unit))
            self.sub_reading.set_label(p.formatted_sub_value)
        self.status.set_label(status)
