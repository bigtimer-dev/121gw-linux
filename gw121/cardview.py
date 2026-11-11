"""The Card and Meter pages: the meter's SD card files and its SETUP settings over Bluetooth (page Ld-bt,
open firmware F-1.05+ / custom C-1.06+). The requests run in a worker thread (gw121/card.py)."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from .card import SETTINGS, CardClient, CardError, CardFile, Setting, settings_values, wire_value
from .link import MeterLink

HOW_TO = "On the meter: SETUP, then REL / PEAK to the page after bt until it shows Ld-bt. Logging must be off."


def needs_newer(version: str | None) -> str | None:
    """Why the meter's firmware cannot do this (None when it can or is unknown)."""
    if not version or len(version) < 6 or version[1] != "-":
        return None
    try:
        key = tuple(int(x) for x in version[2:].split("."))
    except ValueError:
        return None
    if version[0] == "F" and key < (1, 5):
        return f"The meter runs {version}: card and settings over Bluetooth need F-1.05 or later."
    if version[0] == "C" and key < (1, 6):
        return f"The meter runs {version}: card and settings over Bluetooth need C-1.06 or later."
    if version[0] not in "FC":
        return f"The meter runs {version}: card and settings over Bluetooth need the open firmware."
    return None


def format_refused(version: str | None, bootloader: str | None) -> str | None:
    """Why the meter cannot format its card over Bluetooth (None when it can, or when that is not known yet)."""
    if bootloader == "B-EEV":
        return "Formatting over Bluetooth needs zombu4's two-part bootloader (Bootloader… in the main menu)."
    if version and len(version) >= 6 and version[1] == "-":
        try:
            key = tuple(int(x) for x in version[2:].split("."))
        except ValueError:
            return None
        if (version[0] == "F" and key < (1, 9)) or (version[0] == "C" and key < (1, 8)) or version[0] not in "FC":
            return f"The meter runs {version}: formatting over Bluetooth needs F-1.09 / C-1.08 or later."
    return None


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class Worker:
    """One background thread for the meter requests; results come back on the GLib main loop."""

    def __init__(self, link: MeterLink, on_busy: Callable[[bool], None]) -> None:
        self.link = link
        self.on_busy = on_busy
        self.jobs: queue.Queue = queue.Queue()
        self.cancel_flag = threading.Event()
        self.busy = False
        self.client = CardClient(link.card_request, self.cancel_flag.is_set,
                                 getattr(link, "card_stream", None), getattr(link, "firmware_version", None))
        threading.Thread(target=self._run, daemon=True, name="121gw-card").start()

    def submit(self, fn: Callable[[CardClient], object], done: Callable[[object, CardError | None], None]) -> None:
        self.jobs.put((fn, done))

    def cancel(self) -> None:
        self.cancel_flag.set()

    def _run(self) -> None:
        while True:
            fn, done = self.jobs.get()
            self.cancel_flag.clear()
            GLib.idle_add(self._set_busy, True)
            result, error = None, None
            try:
                result = fn(self.client)
            except CardError as e:
                error = e
            except (OSError, ValueError) as e:
                error = CardError(str(e))
            GLib.idle_add(self._finish, done, result, error)

    def _set_busy(self, value: bool) -> bool:
        self.busy = value
        self.link.card_busy = value
        self.on_busy(value)
        return GLib.SOURCE_REMOVE

    def _finish(self, done, result, error) -> bool:
        if self.jobs.empty():
            self._set_busy(False)
        done(result, error)
        return GLib.SOURCE_REMOVE


def _status_page(title: str, icon: str) -> Adw.StatusPage:
    return Adw.StatusPage(title=title, icon_name=icon, vexpand=True)


class CardPage(Gtk.Box):
    """Files on the meter's SD card: list, download, delete, sync to a folder."""

    def __init__(self, win, link: MeterLink, settings, worker: Worker) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.link, self.settings, self.worker = win, link, settings, worker
        self.files: list[CardFile] = []
        self.loaded = False
        self._was_ready = False

        self.banner = Adw.Banner(revealed=False)
        self.append(self.banner)

        self.waiting = _status_page("Put the meter on Ld-bt", "media-flash-symbolic")
        self.waiting.set_description(HOW_TO)

        page = Adw.PreferencesPage()
        top = Adw.PreferencesGroup(title="SD card")
        self.space_row = Adw.ActionRow(title="Free space", subtitle="—")
        self.space_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=160)
        self.space_row.add_suffix(self.space_bar)
        top.add(self.space_row)

        self.folder_row = Adw.ActionRow(title="Local folder", subtitle=str(self.folder()))
        self.folder_row.set_subtitle_selectable(True)
        open_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open folder",
                              css_classes=["flat"])
        open_btn.connect("clicked", lambda *_: self._open_folder())
        pick_btn = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER, css_classes=["flat"])
        pick_btn.connect("clicked", lambda *_: self._pick_folder())
        self.folder_row.add_suffix(open_btn)
        self.folder_row.add_suffix(pick_btn)
        top.add(self.folder_row)

        actions = Gtk.Box(spacing=8, margin_top=12, halign=Gtk.Align.START)
        self.refresh_btn = Gtk.Button(label="Refresh")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        self.sync_btn = Gtk.Button(label="Sync to Folder", css_classes=["suggested-action"],
                                   tooltip_text="Copy new and changed card files into the local folder "
                                                "(nothing is deleted on either side)")
        self.sync_btn.connect("clicked", lambda *_: self.sync())
        self.cancel_btn = Gtk.Button(label="Cancel", visible=False)
        self.cancel_btn.connect("clicked", lambda *_: self.worker.cancel())
        self.format_btn = Gtk.Button(label="Format Card…", css_classes=["destructive-action"],
                                     tooltip_text="Erase the meter's SD card (exFAT, as the meter's page Fr does)")
        self.format_btn.connect("clicked", lambda *_: self.confirm_format())
        for b in (self.refresh_btn, self.sync_btn, self.cancel_btn, self.format_btn):
            actions.append(b)
        top.add(actions)

        self.progress_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=12, visible=False)
        self.progress = Gtk.ProgressBar()
        self.progress_label = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        self.progress_row.append(self.progress)
        self.progress_row.append(self.progress_label)
        top.add(self.progress_row)
        page.add(top)

        self.list_group = Adw.PreferencesGroup(title="Files")
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.list_group.add(self.list_box)
        page.add(self.list_group)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.waiting, "waiting")
        self.stack.add_named(page, "card")
        self.append(self.stack)
        link.connect_changed(self._link_changed)
        self._link_changed()

    # ---- state ----------------------------------------------------------------

    def folder(self) -> Path:
        if self.settings.card_folder:
            return Path(self.settings.card_folder)
        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or str(Path.home() / "Documents")
        return Path(docs) / "121GW card"

    def _link_changed(self) -> None:
        ready = self.link.card_ready
        why = needs_newer(self.link.firmware_version)
        if ready or self.worker.busy:
            self.stack.set_visible_child_name("card")
        else:
            self.stack.set_visible_child_name("waiting")
            if not self.link.is_connected:
                self.waiting.set_title("Not connected")
                self.waiting.set_description("Connect to the meter first, then put it on Ld-bt.\n\n" + HOW_TO)
            else:
                self.waiting.set_title("Put the meter on Ld-bt")
                self.waiting.set_description((why + "\n\n" if why else "") + HOW_TO)
        if ready and not self._was_ready:
            self.loaded = False
            if self.win.stack.get_visible_child_name() == "card":
                self.refresh()
        self._was_ready = ready
        self._buttons()

    def page_shown(self) -> None:
        if self.link.card_ready and not self.loaded and not self.worker.busy:
            self.refresh()

    def busy_changed(self, busy: bool) -> None:
        self.cancel_btn.set_visible(busy and self.progress_row.get_visible())
        if not busy:
            self._link_changed()
        self._buttons()

    def _buttons(self) -> None:
        ok = self.link.card_ready and not self.worker.busy
        self.refresh_btn.set_sensitive(ok)
        self.sync_btn.set_sensitive(ok and bool(self.files))
        why = format_refused(self.link.firmware_version, getattr(self.link, "bootloader", None))
        self.format_btn.set_sensitive(ok and why is None)
        self.format_btn.set_tooltip_text(why or "Erase the meter's SD card (exFAT, as the meter's page Fr does)")
        child = self.list_box.get_first_child()
        while child is not None:
            for b in getattr(child, "buttons", ()):
                b.set_sensitive(ok)
            child = child.get_next_sibling()

    def _error(self, text: str) -> None:
        self.banner.set_title(text)
        self.banner.set_revealed(True)
        GLib.timeout_add_seconds(12, lambda: (self.banner.set_revealed(False), GLib.SOURCE_REMOVE)[1])

    # ---- list -----------------------------------------------------------------

    def refresh(self) -> None:
        if self.worker.busy or not self.link.card_ready:
            return
        self.banner.set_revealed(False)
        self.list_group.set_description("Reading the card…")

        def job(c: CardClient):
            files = c.files()
            try:
                space = c.info()
            except CardError:
                space = None
            return files, space

        self.worker.submit(job, self._listed)

    def _listed(self, result, error: CardError | None) -> None:
        if error:
            self.list_group.set_description("Could not read the card: press Refresh to try again")
            self._error(f"Could not read the card: {error}")
            return
        self.loaded = True
        self.files, space = result
        self.files.sort(key=lambda f: (f.protected, f.name.lower()))
        if space:
            free, total = space
            self.space_row.set_subtitle(f"{human_size(free)} free of {human_size(total)}")
            self.space_bar.set_value((total - free) / total if total else 0)
        self._fill()

    def _fill(self) -> None:
        while (child := self.list_box.get_first_child()) is not None:
            self.list_box.remove(child)
        n = len(self.files)
        size = sum(f.size for f in self.files)
        self.list_group.set_description(f"{n} file{'s' if n != 1 else ''}, {human_size(size)}" if n else
                                        "No files on the card")
        for f in self.files:
            when = f.when.strftime("%Y-%m-%d %H:%M") if f.when else ""
            row = Adw.ActionRow(title=GLib.markup_escape_text(f.name),
                                subtitle=f"{human_size(f.size)}   {when}" + ("   · needed by the meter"
                                                                            if f.protected else ""))
            row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic" if f.protected else
                                     "text-x-generic-symbolic"))
            dl = Gtk.Button(icon_name="folder-download-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Download to the local folder")
            dl.connect("clicked", lambda _b, f=f: self.download(f))
            row.add_suffix(dl)
            buttons = [dl]
            if not f.protected:
                rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Delete from the card")
                rm.connect("clicked", lambda _b, f=f: self.confirm_delete(f))
                row.add_suffix(rm)
                buttons.append(rm)
            row.buttons = buttons
            self.list_box.append(row)
        self._buttons()

    # ---- transfers ------------------------------------------------------------

    def _progress(self, text: str, fraction: float, t0: float, done: int) -> bool:
        self.progress_row.set_visible(True)
        self.cancel_btn.set_visible(True)
        self.progress.set_fraction(max(0.0, min(1.0, fraction)))
        rate = done / max(time.monotonic() - t0, 1e-3)
        damaged = self.link.card_damaged - getattr(self, "_damaged0", 0)
        self.progress_label.set_label(f"{text}   {human_size(rate)}/s" +
                                      (f"   · {damaged} damaged answers asked again" if damaged else ""))
        return GLib.SOURCE_REMOVE

    def download(self, f: CardFile) -> None:
        folder, t0 = self.folder(), time.monotonic()
        self._damaged0 = self.link.card_damaged
        self._progress(f"{f.name}: starting", 0, t0, 0)

        def job(c: CardClient):
            return c.download(f, folder, lambda n: GLib.idle_add(
                self._progress, f"{f.name}: {human_size(n)} of {human_size(f.size)}", n / max(f.size, 1), t0, n))

        def done(path, error):
            self.progress_row.set_visible(False)
            self.cancel_btn.set_visible(False)
            if error:
                self._error(f"Download of {f.name} failed: {error}")
            else:
                self.win.toast(f"{f.name} saved to {folder}", action=("Open Folder", self._open_folder))

        self.worker.submit(job, done)

    def sync(self) -> None:
        folder, t0 = self.folder(), time.monotonic()
        self._damaged0 = self.link.card_damaged
        self._progress("Comparing with the local folder…", 0, t0, 0)

        def job(c: CardClient):
            return c.sync(folder, lambda name, n, total: GLib.idle_add(
                self._progress, f"{name}: {human_size(n)} of {human_size(total)}", n / max(total, 1), t0, n))

        def done(result, error):
            self.progress_row.set_visible(False)
            self.cancel_btn.set_visible(False)
            if error:
                self._error(f"Sync stopped: {error}")
                return
            copied = len(result["copied"])
            text = (f"Copied {copied} file{'s' if copied != 1 else ''} ({human_size(result['bytes'])})"
                    if copied else "Folder already up to date")
            if result["unchanged"]:
                text += f", {result['unchanged']} unchanged"
            self.win.toast(text, action=("Open Folder", self._open_folder))

        self.worker.submit(job, done)

    def confirm_delete(self, f: CardFile) -> None:
        dlg = Adw.AlertDialog(heading=f"Delete {f.name}?",
                              body="The file is removed from the meter's SD card. This cannot be undone.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "delete" and self._delete(f))
        dlg.present(self.win)

    def _delete(self, f: CardFile) -> None:
        def done(_r, error):
            if error:
                self._error(f"Could not delete {f.name}: {error}")
                return
            self.win.toast(f"{f.name} deleted from the card")
            self.loaded = False
            self.refresh()

        self.worker.submit(lambda c: c.delete(f.name), done)

    def confirm_format(self) -> None:
        has_cal = any(f.name.lower() == "cal.bin" for f in self.files)
        body = ("Every file on the meter's SD card is erased and the card is formatted as exFAT, as the meter's "
                "SETUP page Fr does. The calibration inside the meter is not touched.\n\n")
        if has_cal:
            body += ("<b>The card holds cal.bin, the backup of the meter's calibration.</b> Press Sync to Folder first "
                     "so a copy stays on this computer.\n\n")
        body += "Type FORMAT to confirm."
        dlg = Adw.AlertDialog(heading="Format the meter's SD card?", body=body, body_use_markup=True)
        entry = Gtk.Entry(placeholder_text="FORMAT", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("format", "Format")
        dlg.set_response_appearance("format", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_response_enabled("format", False)
        dlg.set_close_response("cancel")
        dlg.set_default_response("cancel")
        entry.connect("changed", lambda e: dlg.set_response_enabled("format", e.get_text() == "FORMAT"))
        dlg.connect("response", lambda _d, r: r == "format" and entry.get_text() == "FORMAT" and self._format())
        dlg.present(self.win)

    def _format(self) -> None:
        self.banner.set_revealed(False)
        self.list_group.set_description("Formatting the card… (the meter answers when it has finished, up to a minute)")
        self.progress_row.set_visible(False)

        def done(_r, error):
            if error:
                self.list_group.set_description(None)
                self._error(f"Format: {error}")
                return
            self.win.toast("The card is formatted")
            self.loaded = False
            self.refresh()

        self.worker.submit(lambda c: c.format(), done)

    # ---- folder ---------------------------------------------------------------

    def _open_folder(self) -> None:
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder))).launch(self.win, None, None, None)

    def _pick_folder(self) -> None:
        dlg = Gtk.FileDialog(title="Folder for card files")
        dlg.set_initial_folder(Gio.File.new_for_path(str(self.folder().parent)))

        def chosen(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f is not None and f.get_path():
                self.settings.card_folder = f.get_path()
                self.folder_row.set_subtitle(f.get_path())

        dlg.select_folder(self.win, None, chosen)


class MeterSettingsPage(Gtk.Box):
    """The meter's SETUP settings, read and changed over Bluetooth; the meter stores them itself."""

    def __init__(self, win, link: MeterLink, worker: Worker) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.link, self.worker = win, link, worker
        self.raw: dict[int, int] = {}
        self.rows: dict[int, Adw.PreferencesRow] = {}
        self._updating = False
        self._was_ready = False
        self.loaded = False

        self.banner = Adw.Banner(revealed=False)
        self.append(self.banner)
        self.waiting = _status_page("Put the meter on Ld-bt", "preferences-system-symbolic")
        self.waiting.set_description(HOW_TO)

        page = Adw.PreferencesPage(description="Changes go to the meter at once and are stored by it, as when "
                                               "they are set on its own SETUP pages.")
        head = Adw.PreferencesGroup()
        reload_btn = Gtk.Button(label="Read Again", halign=Gtk.Align.START)
        reload_btn.connect("clicked", lambda *_: self.refresh())
        self.reload_btn = reload_btn
        head.add(reload_btn)
        page.add(head)
        groups: dict[str, Adw.PreferencesGroup] = {}
        for s in SETTINGS:
            g = groups.get(s.group)
            if g is None:
                g = groups[s.group] = Adw.PreferencesGroup(title=s.group)
                page.add(g)
            row = self._make_row(s)
            self.rows[s.id] = row
            g.add(row)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.waiting, "waiting")
        self.stack.add_named(page, "settings")
        self.append(self.stack)
        link.connect_changed(self._link_changed)
        self._link_changed()

    def _make_row(self, s: Setting) -> Adw.PreferencesRow:
        sub = f"Meter page {s.page}" + (f" · {s.help}" if s.help else "")
        if s.choices:
            row = Adw.ComboRow(title=s.title, subtitle=sub, model=Gtk.StringList.new(list(s.choices)))
            row.connect("notify::selected", lambda r, _p, s=s: self._changed(s, r.get_selected() + s.lo))
        else:
            row = Adw.SpinRow.new_with_range(s.lo, s.hi, 1)
            row.set_title(s.title)
            row.set_subtitle(sub)
            row.set_update_policy(Gtk.SpinButtonUpdatePolicy.IF_VALID)
            row.connect("notify::value", lambda r, _p, s=s: self._spin_changed(s, r))
            if s.id in (0x017, 0x02A, 0x022, 0x02D, 0x02F, 0x018):      # the number alone does not say it: its meaning in the subtitle
                row.connect("notify::value", lambda r, _p, s=s, sub=sub:
                            r.set_subtitle(f"{sub}\n= {s.label(int(r.get_value()))}"))
        row.set_sensitive(False)
        return row

    def _link_changed(self) -> None:
        ready = self.link.card_ready
        why = needs_newer(self.link.firmware_version)
        self.stack.set_visible_child_name("settings" if ready or self.worker.busy else "waiting")
        if not self.link.is_connected:
            self.waiting.set_title("Not connected")
            self.waiting.set_description("Connect to the meter first, then put it on Ld-bt.\n\n" + HOW_TO)
        else:
            self.waiting.set_title("Put the meter on Ld-bt")
            self.waiting.set_description((why + "\n\n" if why else "") + HOW_TO)
        if ready and not self._was_ready:
            self.loaded = False
            if self.win.stack.get_visible_child_name() == "meter":
                self.refresh()
        self._was_ready = ready
        self.busy_changed(self.worker.busy)

    def page_shown(self) -> None:
        if self.link.card_ready and not self.loaded and not self.worker.busy:
            self.refresh()

    def busy_changed(self, busy: bool) -> None:
        ok = self.link.card_ready and not busy
        self.reload_btn.set_sensitive(ok)
        for sid, row in self.rows.items():
            s = next(x for x in SETTINGS if x.id == sid)
            row.set_sensitive(ok and s.wire_id in self.raw)

    def refresh(self) -> None:
        if self.worker.busy or not self.link.card_ready:
            return
        self.worker.submit(lambda c: c.get_settings(), self._got)

    def _got(self, raw, error) -> None:
        if error:
            self.banner.set_title(f"Could not read the settings: {error}")
            self.banner.set_revealed(True)
            return
        self.banner.set_revealed(False)
        self.loaded = True
        self.raw = dict(raw)
        self._show()

    def _show(self) -> None:
        self._updating = True
        values = settings_values(self.raw)
        for s in SETTINGS:
            row = self.rows[s.id]
            if s.id not in values:
                row.set_subtitle("not offered by this firmware")
                continue
            v = values[s.id]
            if isinstance(row, Adw.ComboRow):
                if s.lo <= v <= s.hi:
                    row.set_selected(v - s.lo)
            else:
                row.set_value(v)
        self._updating = False
        self.busy_changed(self.worker.busy)

    def _spin_changed(self, s: Setting, row: Adw.SpinRow) -> None:
        if self._updating:
            return
        # a spin row sends a value per click: send the last one after a short pause
        token = object()
        row._pending = token
        GLib.timeout_add(600, lambda: (row._pending is token and self._changed(s, int(row.get_value())),
                                       GLib.SOURCE_REMOVE)[1])

    def _changed(self, s: Setting, value: int) -> None:
        if self._updating or s.wire_id not in self.raw:
            return
        current = settings_values(self.raw).get(s.id)
        if current == value:
            return
        try:
            byte = wire_value(s, value, self.raw)
        except CardError as e:
            self.banner.set_title(str(e))
            self.banner.set_revealed(True)
            self._show()
            return

        def done(now, error):
            if error:
                self.banner.set_title(f"{s.title}: not changed ({error})")
                self.banner.set_revealed(True)
            else:
                self.raw[s.wire_id] = now
                self.banner.set_revealed(False)
                self.win.toast(f"{s.title}: {s.label((now >> s.shift) & s.mask)}")
            self._show()

        self.worker.submit(lambda c: c.put_setting(s.wire_id, byte, s.wide), done)
