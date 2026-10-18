"""Main window: Live / Sessions / Connect, mirroring the iOS / Windows / Android apps."""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .chart import TrendChart, export_png, fmt_elapsed, fmt_num
from .continuity import ContinuityEngine
from .keys import KEYPAD, MeterKey
from .lcd import LcdView
from .link import MeterLink
from .packet import PacketV2
from .sessions import DataPoint, Session, SessionStore, default_session_name
from .settings import Settings

PREVIEW_POINTS = 600


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "session"


def _card(child: Gtk.Widget, margin: int = 12) -> Gtk.Widget:
    frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    frame.add_css_class("card")
    child.set_margin_top(margin)
    child.set_margin_bottom(margin)
    child.set_margin_start(margin)
    child.set_margin_end(margin)
    frame.append(child)
    return frame


class MeterButton(Gtk.Button):
    """Short press on release, long press after the key's hold time (MODE long ≈ backlight)."""

    def __init__(self, key: MeterKey, on_key):
        super().__init__()
        self.key = key
        self.on_key = on_key
        self._timer = 0
        self._long_fired = False
        self.add_css_class("meter-key")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label=key.title)
        self.arrow = Gtk.Label(label=key.setup_arrow or "")
        self.arrow.add_css_class("arrow")
        self.arrow.set_visible(bool(key.setup_arrow))
        box.append(self.title)
        box.append(self.arrow)
        self.set_child(box)
        self.set_tooltip_text(f"{key.title}  —  key '{key.shortcut.upper()}', Shift+{key.shortcut.upper()} = long press")

        gesture = Gtk.GestureClick()
        gesture.connect("pressed", self._pressed)
        gesture.connect("released", self._released)
        gesture.connect("cancel", lambda *_: self._cancel())
        self.add_controller(gesture)

    def set_setup_mode(self, in_setup: bool) -> None:
        if self.key.setup_arrow:
            self.arrow.set_opacity(1.0 if in_setup else 0.45)

    def _pressed(self, *_):
        self._cancel()
        self._long_fired = False
        self.add_css_class("pressed")

        def fire():
            self._timer = 0
            self._long_fired = True
            self.on_key(self.key, True)
            return GLib.SOURCE_REMOVE

        self._timer = GLib.timeout_add(self.key.long_press_ms, fire)

    def _released(self, *_):
        was_long = self._long_fired
        self._cancel()
        if not was_long:
            self.on_key(self.key, False)

    def _cancel(self):
        self.remove_css_class("pressed")
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, link: MeterLink, settings: Settings, store: SessionStore):
        super().__init__(application=app, title="121GW")
        self.set_default_size(540, 940)
        self.set_size_request(380, 560)
        self.link = link
        self.settings = settings
        self.store = store
        self.continuity = ContinuityEngine(settings)
        self.preview: deque[DataPoint] = deque(maxlen=PREVIEW_POINTS)
        self._preview_fp: str | None = None
        self._last_synced_beep: float | None = None
        self._dialog_open = False
        self._notified: set[str] = set()
        self._device_rows: list[Gtk.Widget] = []
        self._session_rows: list[Gtk.Widget] = []

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._build_live(), "live", "Live", "utilities-system-monitor-symbolic")
        self.stack.add_titled_with_icon(self._build_sessions(), "sessions", "Sessions", "document-open-recent-symbolic")
        self.stack.add_titled_with_icon(self._build_connect(), "connect", "Connect", "bluetooth-symbolic")
        self.toasts.set_child(self.stack)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("Keyboard Shortcuts", "win.shortcuts")
        menu.append("About 121GW", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main menu"))

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(self.toasts)
        self.set_content(tv)

        act = Gio.SimpleAction.new("shortcuts", None)
        act.connect("activate", lambda *_: self._show_shortcuts())
        self.add_action(act)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        link.connect_packet(self._on_packet)
        link.connect_changed(self.refresh)
        self.refresh()
        self._refresh_sessions()
        self._refresh_log_ui()

    # =====================================================================
    # Live page
    # =====================================================================

    def _build_live(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.CenterBox()
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        bar.set_margin_top(6)
        bar.set_margin_bottom(4)
        self.rssi_label = Gtk.Label()
        self.rssi_label.add_css_class("statusbar-label")
        self.name_label = Gtk.Label()
        self.name_label.add_css_class("heading")
        right = Gtk.Box(spacing=10)
        self.battery_label = Gtk.Label()
        self.battery_label.add_css_class("statusbar-label")
        self.time_label = Gtk.Label()
        self.time_label.add_css_class("statusbar-label")
        right.append(self.battery_label)
        right.append(self.time_label)
        bar.set_start_widget(self.rssi_label)
        bar.set_center_widget(self.name_label)
        bar.set_end_widget(right)
        outer.append(bar)

        self.banner = Adw.Banner()
        outer.append(self.banner)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(24)
        box.set_margin_start(14)
        box.set_margin_end(14)

        self.lcd = LcdView()
        box.append(self.lcd)

        # meter controls
        ctl = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        t = Gtk.Label(label="Meter Controls", xalign=0)
        t.add_css_class("card-title")
        ctl.append(t)
        grid = Gtk.Grid(column_spacing=6, row_spacing=6, column_homogeneous=True)
        self.key_buttons: list[MeterButton] = []
        for i, key in enumerate(KEYPAD):
            b = MeterButton(key, self._send_key)
            self.key_buttons.append(b)
            grid.attach(b, i % 4, i // 4, 1, 1)
        ctl.append(grid)
        box.append(_card(ctl, 10))

        # trend
        trend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head = Gtk.Box(spacing=6)
        self.trend_title = Gtk.Label(label="Trend", xalign=0)
        self.trend_title.add_css_class("card-title")
        head.append(self.trend_title)
        head.append(Gtk.Box(hexpand=True))
        self.trend_stats = Gtk.Label(xalign=1)
        self.trend_stats.add_css_class("mono")
        self.trend_stats.add_css_class("dim-label")
        self.trend_stats.add_css_class("caption")
        head.append(self.trend_stats)
        trend.append(head)

        opts = Gtk.Box(spacing=8)
        self.temp_toggle = Gtk.Button(has_frame=False)
        self.temp_toggle.add_css_class("temp-toggle")
        self.temp_toggle.add_css_class("caption")
        self.temp_toggle.connect("clicked", self._toggle_temperature)
        self.temp_toggle.set_visible(False)
        opts.append(self.temp_toggle)
        opts.append(Gtk.Box(hexpand=True))
        self.autoscale_btn = Gtk.ToggleButton(label="Auto Y", has_frame=False,
                                              tooltip_text="Fit the Y axis to the data instead of the meter range")
        self.autoscale_btn.add_css_class("caption")
        self.autoscale_btn.connect("toggled", self._toggle_autoscale)
        opts.append(self.autoscale_btn)
        reset = Gtk.Button(icon_name="zoom-original-symbolic", has_frame=False, tooltip_text="Reset zoom")
        reset.connect("clicked", lambda *_: self.live_chart.reset_view())
        opts.append(reset)
        trend.append(opts)

        self.live_chart = TrendChart(220)
        self.live_chart.show_temperature = self.settings.show_temperature
        trend.append(self.live_chart)
        hint = Gtk.Label(label="Y axis follows meter mode/range · wheel to zoom · drag to pan · double-click to reset",
                         wrap=True, xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        trend.append(hint)
        box.append(_card(trend))

        self.log_button = Gtk.Button(label="Start Log", halign=Gtk.Align.CENTER)
        self.log_button.add_css_class("pill")
        self.log_button.set_size_request(200, -1)
        self.log_button.connect("clicked", lambda *_: self._toggle_log())
        box.append(self.log_button)
        self.sample_count = Gtk.Label()
        self.sample_count.add_css_class("dim-label")
        box.append(self.sample_count)

        clamp = Adw.Clamp(maximum_size=760, child=box)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
        outer.append(scroll)
        return outer

    # =====================================================================
    # Sessions page
    # =====================================================================

    def _build_sessions(self) -> Gtk.Widget:
        self.sessions_stack = Gtk.Stack()
        empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", title="No sessions yet",
                               description="Start a log on the Live tab.")
        self.sessions_stack.add_named(empty, "empty")

        page = Adw.PreferencesPage()
        self.sessions_group = Adw.PreferencesGroup(title="Saved sessions")
        delete_all = Gtk.Button(label="Delete All", valign=Gtk.Align.CENTER)
        delete_all.add_css_class("destructive-action")
        delete_all.add_css_class("flat")
        delete_all.connect("clicked", lambda *_: self._confirm_delete_all())
        self.sessions_group.set_header_suffix(delete_all)
        page.add(self.sessions_group)
        self.sessions_stack.add_named(page, "list")
        return self.sessions_stack

    def _refresh_sessions(self) -> None:
        for r in self._session_rows:
            self.sessions_group.remove(r)
        self._session_rows.clear()
        self.sessions_stack.set_visible_child_name("list" if self.store.sessions else "empty")
        for s in self.store.sessions:
            st = s.stats()
            sub = f"{len(s.points)} pts · {datetime.fromtimestamp(s.created_at):%Y-%m-%d %H:%M}" \
                  f" · {fmt_elapsed(s.duration)}"
            if st:
                sub += f" · avg {fmt_num(st['avg'])} {s.graph_unit}"
            row = Adw.ActionRow(title=GLib.markup_escape_text(s.name), subtitle=sub, activatable=True)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, sess=s: SessionDialog(self, sess).present(self))
            self.sessions_group.add(row)
            self._session_rows.append(row)

    def _confirm_delete_all(self) -> None:
        if not self.store.sessions:
            return
        d = Adw.AlertDialog(heading="Delete all sessions?", body="This cannot be undone.")
        d.add_response("cancel", "Cancel")
        d.add_response("delete", "Delete All")
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, resp):
            if resp == "delete":
                self.store.clear_all()
                self._refresh_sessions()

        d.connect("response", done)
        d.present(self)

    # =====================================================================
    # Connect page
    # =====================================================================

    def _build_connect(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Connection")
        self.conn_row = Adw.ActionRow(title="Disconnected")
        self.conn_icon = Gtk.Image(icon_name="bluetooth-disabled-symbolic")
        self.conn_row.add_prefix(self.conn_icon)
        g.add(self.conn_row)
        self.power_row = Adw.ButtonRow(title="Turn On Bluetooth", start_icon_name="bluetooth-active-symbolic",
                                       visible=False)
        self.power_row.add_css_class("suggested-action")
        self.power_row.connect("activated", lambda *_: self.link.power_on())
        g.add(self.power_row)
        self.auto_row = Adw.SwitchRow(title="Auto-reconnect", subtitle="Reconnect to the last meter automatically",
                                      active=self.settings.auto_reconnect)
        self.auto_row.connect("notify::active", lambda r, _p: self.link.set_auto_reconnect(r.get_active()))
        g.add(self.auto_row)
        page.add(g)

        self.dev_group = Adw.PreferencesGroup(title="Devices")
        self.scan_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.scan_button.add_css_class("flat")
        self.scan_button.connect("clicked", lambda *_: self.link.stop_scan() if self.link.is_scanning
                                 else self.link.start_scan())
        self.scan_spinner = Adw.Spinner(visible=False)
        sfx = Gtk.Box(spacing=6)
        sfx.append(self.scan_spinner)
        sfx.append(self.scan_button)
        self.dev_group.set_header_suffix(sfx)
        self.no_devices = Adw.ActionRow(title="No 121GW found yet",
                                        subtitle="Hold 1ms PEAK on the meter until the BT icon appears, then scan.")
        self.dev_group.add(self.no_devices)
        show_all = Adw.SwitchRow(title="Show all BLE devices",
                                 subtitle="Use if the meter advertises without its name",
                                 active=self.settings.show_all_devices)
        show_all.connect("notify::active", self._on_show_all)
        self.dev_group.add(show_all)
        self._show_all_row = show_all
        page.add(self.dev_group)

        dg = Adw.PreferencesGroup()
        self.disconnect_btn = Gtk.Button(label="Disconnect", halign=Gtk.Align.CENTER)
        self.disconnect_btn.add_css_class("destructive-action")
        self.disconnect_btn.add_css_class("pill")
        self.disconnect_btn.connect("clicked", lambda *_: self.link.disconnect())
        dg.add(self.disconnect_btn)
        page.add(dg)

        cg = Adw.PreferencesGroup(
            title="Continuity Feedback",
            description="Beep in Continuity/Ω when below ON, stop above OFF. Also beeps in Diode mode while a "
                        "junction is conducting. Defaults: ON &lt; 25 Ω, OFF &gt; 400 Ω. The ON threshold follows the "
                        "meter's own beeper setting when it reports one.")
        snd = Adw.SwitchRow(title="Sound (beep)", active=self.settings.cont_sound)
        snd.connect("notify::active", lambda r, _p: setattr(self.settings, "cont_sound", r.get_active()))
        cg.add(snd)
        self.on_row = Adw.SpinRow.new_with_range(0, 100_000, 1)
        self.on_row.set_title("ON below (Ω)")
        self.on_row.set_digits(1)
        self.off_row = Adw.SpinRow.new_with_range(1, 1_000_000, 1)
        self.off_row.set_title("OFF above (Ω)")
        self.off_row.set_digits(1)
        self._load_thresholds()
        self.on_row.connect("notify::value", self._thresholds_changed)
        self.off_row.connect("notify::value", self._thresholds_changed)
        cg.add(self.on_row)
        cg.add(self.off_row)
        test = Adw.ButtonRow(title="Test beep")
        test.connect("activated", lambda *_: self.continuity.beeper.beep())
        cg.add(test)
        page.add(cg)

        ng = Adw.PreferencesGroup(title="Desktop")
        notif = Adw.SwitchRow(title="Notifications", subtitle="Communication lost and low meter battery",
                              active=self.settings.notifications)
        notif.connect("notify::active", lambda r, _p: setattr(self.settings, "notifications", r.get_active()))
        ng.add(notif)
        page.add(ng)

        hg = Adw.PreferencesGroup(
            description="Turn the meter's Bluetooth on by holding the 1ms PEAK key until the BT icon appears. "
                        "Long-press a meter button for its hold-key function (MODE long = backlight).")
        page.add(hg)
        return page

    def _load_thresholds(self) -> None:
        self._loading_thresholds = True
        self.on_row.set_value(self.settings.cont_on_threshold)
        self.off_row.set_value(self.settings.cont_off_threshold)
        self._loading_thresholds = False

    def _thresholds_changed(self, *_):
        if getattr(self, "_loading_thresholds", False):
            return
        self.settings.cont_on_threshold = self.on_row.get_value()
        self.settings.cont_off_threshold = self.off_row.get_value()
        self.settings.normalize_thresholds()
        self.settings.save()
        if self.off_row.get_value() != self.settings.cont_off_threshold:
            GLib.idle_add(lambda: self._load_thresholds() or GLib.SOURCE_REMOVE)

    def _on_show_all(self, row, _p):
        self.settings.show_all_devices = row.get_active()
        if not row.get_active():
            for addr in [a for a, d in self.link.devices.items() if not d.is_meter and not d.connected]:
                self.link.devices.pop(addr, None)
        self.refresh()

    def _refresh_devices(self) -> None:
        for r in self._device_rows:
            self.dev_group.remove(r)
        self._device_rows.clear()
        devices = sorted(self.link.devices.values(), key=lambda d: (not d.is_meter, -(d.rssi or -999)))
        self.no_devices.set_visible(not devices)
        for d in devices:
            connected = self.link.is_connected and d.address == self.link.connected_address
            sub = d.address + (f" · {d.rssi} dBm" if d.rssi is not None else "")
            row = Adw.ActionRow(title=GLib.markup_escape_text(d.name), subtitle=sub, activatable=not connected)
            row.add_prefix(Gtk.Image(icon_name="bluetooth-active-symbolic" if connected else "bluetooth-symbolic"))
            if connected:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            elif self.link.connecting and d.address == self.link.connected_address:
                row.add_suffix(Adw.Spinner())
            row.connect("activated", lambda _r, a=d.address: self.link.connect_device(a))
            self.dev_group.add(row)
            self._device_rows.append(row)
        # keep the "show all" switch at the bottom
        self.dev_group.remove(self._show_all_row)
        self.dev_group.add(self._show_all_row)

    # =====================================================================
    # Live updates
    # =====================================================================

    def _on_packet(self, pkt: PacketV2) -> None:
        if not self.store.append(pkt):
            self._function_changed()
        self.continuity.evaluate(pkt)
        self._sync_continuity_threshold(pkt)

        if math.isfinite(pkt.main_value):
            fp = pkt.measure_fingerprint
            if fp != self._preview_fp:
                self.preview.clear()
                self._preview_fp = fp
            self.preview.append(DataPoint.from_packet(pkt))

        self.live_chart.empty_text = ("Overload (OL) – nothing to plot" if pkt.is_main_overflow
                                      else "No samples yet")
        if self.store.is_logging:
            self.live_chart.set_data(self.store.live_window, pkt.graph_unit)
        else:
            self.live_chart.set_data(list(self.preview), pkt.graph_unit)
        self._refresh_log_ui()

    def _sync_continuity_threshold(self, pkt: PacketV2) -> None:
        t = pkt.continuity_beeper_ohms
        if t is None:
            return
        if self._last_synced_beep == t and abs(self.settings.cont_on_threshold - t) < 0.5:
            return
        self._last_synced_beep = t
        if abs(self.settings.cont_on_threshold - t) < 0.5:
            return
        self.settings.cont_on_threshold = t
        if self.settings.cont_off_threshold < t + 1:
            self.settings.cont_off_threshold = max(400.0, t + 50)
        self.settings.normalize_thresholds()
        self.settings.save()
        self._load_thresholds()
        self.toast(f"Continuity ON below set to {t:.0f} Ω from the meter's beeper")

    def refresh(self) -> None:
        link = self.link
        p = link.latest

        # header row
        if link.is_connected:
            self.rssi_label.set_label(f"BT  {link.rssi} dBm" if link.rssi is not None else "BT")
            self.rssi_label.remove_css_class("dim-label")
        else:
            self.rssi_label.set_label("BT off")
            self.rssi_label.add_css_class("dim-label")
        self.name_label.set_label(link.connected_name or "121GW")
        if link.battery_voltage is not None:
            self.battery_label.set_label(f"{link.battery_voltage:.2f}V")
            if link.battery_low:
                self.battery_label.add_css_class("error")
            else:
                self.battery_label.remove_css_class("error")
        else:
            self.battery_label.set_label("")
        self.time_label.set_label(link.meter_time or "")

        # warnings
        warn = None
        if link.communication_lost and (link.is_connected or link.settings.auto_reconnect or link.latest):
            warn = ("lost", "121GW communication lost")
        elif link.battery_near_power_off:
            warn = ("battery", f"Meter battery low: {link.battery_voltage:.2f} V — near power-off")
        elif link.battery_low:
            warn = ("battery", f"Meter battery low: {link.battery_voltage:.2f} V"
                    if link.battery_voltage is not None else "Meter battery low")
        if warn:
            self.banner.set_title(warn[1])
            self.banner.set_revealed(True)
            self._notify(warn[0], warn[1])
        else:
            self.banner.set_revealed(False)
            self._notified.clear()

        # LCD
        if p is not None:
            self.lcd.update(p, in_setup_menu=link.in_setup_menu, status=link.status_text)
            self.temp_toggle.set_visible(p.sub_is_temperature)
            self._update_temp_toggle(p.sub_unit)
        else:
            self.lcd.clear(link.status_text)
        for b in self.key_buttons:
            b.set_sensitive(link.is_connected)
            b.set_setup_mode(link.in_setup_menu)

        # connect page
        if link.is_connected:
            title, icon = f"Connected · {link.connected_name or ''}", "bluetooth-active-symbolic"
        elif link.connecting:
            title, icon = "Connecting…", "bluetooth-acquiring-symbolic"
        else:
            title, icon = "Disconnected", "bluetooth-disabled-symbolic"
        self.conn_row.set_title(GLib.markup_escape_text(title))
        self.conn_row.set_subtitle(GLib.markup_escape_text(
            f"{link.status_text} · {link.packets_received} packets" if link.packets_received else link.status_text))
        self.conn_icon.set_from_icon_name(icon)
        self.power_row.set_visible(link.adapter_off)
        if self.auto_row.get_active() != link.settings.auto_reconnect:
            self.auto_row.set_active(link.settings.auto_reconnect)
        self.scan_button.set_label("Stop Scan" if link.is_scanning else "Scan")
        self.scan_button.set_sensitive(not link.is_connected)
        self.scan_spinner.set_visible(link.is_scanning)
        self.disconnect_btn.set_sensitive(link.is_connected or link.connecting)
        self._refresh_devices()
        self._refresh_log_ui()

    def _notify(self, key: str, text: str) -> None:
        if key in self._notified or not self.settings.notifications:
            return
        self._notified.add(key)
        if self.is_active():
            return
        n = Gio.Notification.new("121GW")
        n.set_body(text)
        n.set_priority(Gio.NotificationPriority.HIGH)
        self.get_application().send_notification(f"gw121-{key}", n)

    def _update_temp_toggle(self, unit: str) -> None:
        if self.live_chart.show_temperature:
            self.temp_toggle.set_label(f"Temperature monitoring: {unit}")
            self.temp_toggle.remove_css_class("off")
        else:
            self.temp_toggle.set_label("Temperature monitoring: hidden")
            self.temp_toggle.add_css_class("off")

    def _toggle_temperature(self, *_):
        self.live_chart.show_temperature = not self.live_chart.show_temperature
        self.settings.show_temperature = self.live_chart.show_temperature
        self.live_chart.queue_draw()
        self._update_temp_toggle(self.link.latest.sub_unit if self.link.latest else "°C")

    def _toggle_autoscale(self, btn):
        self.live_chart.autoscale = btn.get_active()
        self.live_chart.queue_draw()

    def _refresh_log_ui(self) -> None:
        s = self.store
        if s.is_logging:
            self.log_button.set_label("Stop & Save")
            self.log_button.remove_css_class("suggested-action")
            self.log_button.add_css_class("destructive-action")
            self.sample_count.set_label(f"{len(s.current)} samples")
            self.trend_title.set_label("Trend · logging")
        else:
            self.log_button.set_label("Start Log")
            self.log_button.remove_css_class("destructive-action")
            self.log_button.add_css_class("suggested-action")
            self.sample_count.set_label("")
            self.trend_title.set_label("Trend · preview" if self.preview else "Trend")
        self.log_button.set_sensitive(self.link.is_connected or s.is_logging)
        if s.is_logging and s.live_min is not None and s.live_max is not None:
            unit = s.current[-1].plot_unit if s.current else ""
            self.trend_stats.set_label(f"min {fmt_num(s.live_min)}  max {fmt_num(s.live_max)} {unit}")
        elif not s.is_logging and self.preview:
            vals = [p.plot_value for p in self.preview if math.isfinite(p.plot_value)]
            if vals:
                self.trend_stats.set_label(f"min {fmt_num(min(vals))}  max {fmt_num(max(vals))} "
                                           f"{self.preview[-1].plot_unit}")
        else:
            self.trend_stats.set_label("")

    # =====================================================================
    # Logging
    # =====================================================================

    def _toggle_log(self) -> None:
        if self.store.is_logging:
            self.store.stop_logging()
            if self.store.current:
                self._prompt_save(resume=False)
        else:
            self.store.start_logging()
            self.live_chart.reset_view()
            self.live_chart.set_data([], "")
        self._refresh_log_ui()

    def _function_changed(self) -> None:
        if self._dialog_open:
            return
        self._dialog_open = True
        d = Adw.AlertDialog(
            heading="Measurement function changed",
            body="Trend logging stopped because the meter measurement function changed. Range changes are kept "
                 "in the same log and normalized for the graph.\n\nSave the current log?")
        d.add_response("discard", "Discard")
        d.add_response("save", "Save")
        d.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("save")

        def done(_d, resp):
            self._dialog_open = False
            if resp == "save":
                self._prompt_save(resume=True)
            else:
                self.store.discard_current()
                self.store.start_logging()
                self._refresh_log_ui()

        d.connect("response", done)
        d.present(self)

    def _prompt_save(self, resume: bool) -> None:
        self._dialog_open = True
        d = Adw.AlertDialog(heading="Save Session", body=f"{len(self.store.current)} samples")
        entry = Gtk.Entry(text=default_session_name(), activates_default=True)
        d.set_extra_child(entry)
        d.add_response("discard", "Discard")
        d.add_response("save", "Save")
        d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        d.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("save")
        d.set_close_response("discard")

        def done(_d, resp):
            self._dialog_open = False
            if resp == "save":
                name = entry.get_text().strip() or default_session_name()
                self.store.save_current(name)
                self._refresh_sessions()
                self.toast(f"Saved “{name}”")
            else:
                self.store.discard_current()
            if resume:
                self.store.start_logging()
            self._refresh_log_ui()

        d.connect("response", done)
        d.present(self)
        entry.grab_focus()

    # =====================================================================
    # Keys / misc
    # =====================================================================

    def _send_key(self, key: MeterKey, long_press: bool) -> None:
        self.link.send_key(key, long_press)

    def _on_key(self, _ctl, keyval, _code, state) -> bool:
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Text, Gtk.TextView, Gtk.Entry)) or self.get_visible_dialog():
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        name = (Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or "").lower()
        if ctrl:
            if name == "l":
                self._toggle_log()
                return True
            pages = {"1": "live", "2": "sessions", "3": "connect"}
            if name in pages:
                self.stack.set_visible_child_name(pages[name])
                return True
            return False
        if state & (Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK):
            return False
        for key in KEYPAD:
            if name == key.shortcut:
                self.link.send_key(key, shift)
                return True
        if name == "up" and self.link.in_setup_menu:
            self.link.send_key(MeterKey.REL)
            return True
        if name == "down" and self.link.in_setup_menu:
            self.link.send_key(MeterKey.PEAK)
            return True
        return False

    def _show_shortcuts(self) -> None:
        lines = "\n".join(f"<b>{k.shortcut.upper()}</b>\t{k.title}" for k in KEYPAD)
        body = (f"{lines}\n\n<b>Shift</b>+key\tlong press\n<b>↑ / ↓</b>\tREL / PEAK in SETUP\n"
                "<b>Ctrl+L</b>\tstart / stop log\n<b>Ctrl+1/2/3</b>\tLive / Sessions / Connect\n<b>Ctrl+Q</b>\tquit")
        d = Adw.AlertDialog(heading="Keyboard Shortcuts", body=body, body_use_markup=True)
        d.add_response("ok", "Close")
        d.present(self)

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=3))


class SessionDialog(Adw.Dialog):
    def __init__(self, win: MainWindow, session: Session):
        super().__init__(title=session.name, content_width=720, content_height=820)
        self.win = win
        self.session = session
        self.store = win.store

        header = Adw.HeaderBar()
        rename = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Rename")
        rename.connect("clicked", lambda *_: self._rename())
        header.pack_start(rename)
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete session")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self._delete())
        header.pack_end(delete)

        self.toasts = Adw.ToastOverlay()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(16)
        box.set_margin_end(16)

        self.chart = TrendChart(300)
        self.chart.set_data(session.points, session.graph_unit)
        chart_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        opts = Gtk.Box(spacing=6)
        self.temp_btn = Gtk.Button(has_frame=False, visible=session.has_temperature)
        self.temp_btn.add_css_class("temp-toggle")
        self.temp_btn.add_css_class("caption")
        self.temp_btn.connect("clicked", self._toggle_temp)
        self._update_temp()
        opts.append(self.temp_btn)
        opts.append(Gtk.Box(hexpand=True))
        auto = Gtk.ToggleButton(label="Auto Y", has_frame=False)
        auto.add_css_class("caption")
        auto.connect("toggled", lambda b: (setattr(self.chart, "autoscale", b.get_active()), self.chart.queue_draw()))
        opts.append(auto)
        chart_box.append(opts)
        chart_box.append(self.chart)
        box.append(_card(chart_box))

        stats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        t = Gtk.Label(label="Statistics", xalign=0)
        t.add_css_class("heading")
        stats.append(t)
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, min_children_per_line=2,
                           column_spacing=6, row_spacing=6, homogeneous=True)
        u = session.graph_unit
        st = session.stats()
        items = [("Samples", str(len(session.points))), ("Duration", fmt_elapsed(session.duration)),
                 ("Started", datetime.fromtimestamp(session.points[0].timestamp if session.points
                                                    else session.created_at).strftime("%Y-%m-%d %H:%M:%S"))]
        if st:
            items += [("Min", f"{fmt_num(st['min'])} {u}"), ("Max", f"{fmt_num(st['max'])} {u}"),
                      ("Average", f"{fmt_num(st['avg'])} {u}"), ("Peak-Peak", f"{fmt_num(st['pp'])} {u}"),
                      ("Std dev", f"{fmt_num(st['std'])} {u}")]
            if len(session.points) > 1 and session.duration > 0:
                items.append(("Rate", f"{(len(session.points) - 1) / session.duration:.2f} /s"))
        for title, value in items:
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            tile.add_css_class("stat-tile")
            a = Gtk.Label(label=title, xalign=0)
            a.add_css_class("stat-title")
            b = Gtk.Label(label=value, xalign=0, selectable=True)
            b.add_css_class("stat-value")
            tile.append(a)
            tile.append(b)
            grid.append(tile)
        stats.append(grid)
        box.append(_card(stats))

        notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        t = Gtk.Label(label="Notes", xalign=0)
        t.add_css_class("heading")
        notes.append(t)
        self.notes_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                                       left_margin=6, right_margin=6)
        self.notes_view.get_buffer().set_text(session.notes)
        self.notes_view.get_buffer().connect("changed", self._notes_changed)
        self.notes_view.set_size_request(-1, 90)
        sw = Gtk.ScrolledWindow(child=self.notes_view, min_content_height=90)
        sw.add_css_class("view")
        notes.append(sw)
        box.append(_card(notes))

        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        csv = Gtk.Button(label="Export CSV")
        csv.add_css_class("pill")
        csv.connect("clicked", lambda *_: self._export("csv"))
        png = Gtk.Button(label="Export Graph")
        png.add_css_class("pill")
        png.connect("clicked", lambda *_: self._export("png"))
        btns.append(csv)
        btns.append(png)
        box.append(btns)

        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, child=box)
        self.toasts.set_child(scroll)
        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.set_content(self.toasts)
        self.set_child(tv)
        self._notes_timer = 0
        self.connect("closed", lambda *_: self._flush_notes())

    def _update_temp(self) -> None:
        if self.chart.show_temperature:
            self.temp_btn.set_label(f"Temperature monitoring: {self.session.temperature_unit or '°C'}")
            self.temp_btn.remove_css_class("off")
        else:
            self.temp_btn.set_label("Temperature monitoring: hidden")
            self.temp_btn.add_css_class("off")

    def _toggle_temp(self, *_):
        self.chart.show_temperature = not self.chart.show_temperature
        self.chart.queue_draw()
        self._update_temp()

    def _notes_changed(self, _buf):
        if self._notes_timer:
            GLib.source_remove(self._notes_timer)
        self._notes_timer = GLib.timeout_add(600, self._flush_notes)

    def _flush_notes(self):
        if self._notes_timer:
            GLib.source_remove(self._notes_timer)
            self._notes_timer = 0
        buf = self.notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if text != self.session.notes:
            self.store.update(self.session, notes=text)
        return GLib.SOURCE_REMOVE

    def _rename(self) -> None:
        d = Adw.AlertDialog(heading="Rename Session")
        entry = Gtk.Entry(text=self.session.name, activates_default=True)
        d.set_extra_child(entry)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Rename")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")

        def done(_d, resp):
            name = entry.get_text().strip()
            if resp == "ok" and name:
                self.store.update(self.session, name=name)
                self.set_title(name)
                self.win._refresh_sessions()

        d.connect("response", done)
        d.present(self)

    def _delete(self) -> None:
        d = Adw.AlertDialog(heading="Delete session?", body=self.session.name)
        d.add_response("cancel", "Cancel")
        d.add_response("delete", "Delete")
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, resp):
            if resp == "delete":
                self.store.delete(self.session)
                self.win._refresh_sessions()
                self.force_close()

        d.connect("response", done)
        d.present(self)

    def _export(self, kind: str) -> None:
        fd = Gtk.FileDialog(title="Export CSV" if kind == "csv" else "Export Graph",
                            initial_name=f"{_safe_filename(self.session.name)}.{kind}")
        f = Gtk.FileFilter()
        f.set_name("CSV" if kind == "csv" else "PNG image")
        f.add_suffix(kind)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        fd.set_filters(filters)

        def done(dialog, res):
            try:
                gfile = dialog.save_finish(res)
            except GLib.Error:
                return
            path = gfile.get_path()
            try:
                if kind == "csv":
                    with open(path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(self.session.csv_string())
                else:
                    export_png(path, self.session.points, self.session.graph_unit,
                               f"{self.session.name} · {self.session.graph_unit}", self.chart.show_temperature)
            except OSError as e:
                self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(f"Export failed: {e}")))
                return
            toast = Adw.Toast(title=GLib.markup_escape_text(f"Saved {gfile.get_basename()}"),
                              button_label="Open", timeout=5)
            toast.connect("button-clicked",
                          lambda *_: Gtk.FileLauncher.new(gfile).launch(self.win, None, None, None))
            self.toasts.add_toast(toast)

        fd.save(self.win, None, done)
