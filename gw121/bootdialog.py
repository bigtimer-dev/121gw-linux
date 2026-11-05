"""Install or update zombu4's two-part bootloader (releases bl-vA.B.C of zombu4/121GW-Open-Firmware).

Two routes, chosen by what the meter reports to the bootloader request 0x92 (firmware v1.0.4 / C-1.04 and later):
  - update: the meter already has zombu4's bootloader (B-A.BC). The firmware restarts into the update mode (0x91) and
    the bootloader takes blimage.bin into its spare slot (START kind 2 + version); the meter restarts and the starter
    copies it in. The running bootloader stays until the copy, so an interrupted transfer changes nothing.
  - first install: the meter has EEVblog's bootloader (B-EEV). On the SETUP page Ld the firmware takes install.bin
    (START kind 1 of the card upload), checks the battery (4.80 V) and EEVblog's bootloader, writes the starter and the
    bootloader, and restarts. About 10 ms of that (the starter's first page) must not be interrupted by a power cut.
Either way the data EEPROM (calibration, serial number, settings) is never written.
"""

from __future__ import annotations

import hashlib
import threading
import time

from gi.repository import Adw, GLib, Gtk

from . import releases
from .settings import data_dir
from .upload import KIND_BOOTLOADER, KIND_INSTALL

VERIFY_S = 120                      # after the transfer: wait this long for the meter to report the new bootloader
BATTERY_MIN = 4.80


class BootloaderDialog(Adw.Dialog):
    def __init__(self, win) -> None:
        super().__init__(title="Bootloader", content_width=540)
        self.win, self.link = win, win.link
        self.release: releases.BootloaderRelease | None = None
        self.files: tuple[bytes, bytes] | None = None
        self.busy = False
        self._all_sent = False
        self._verify_until = 0.0
        self._target: str | None = None
        self._first_route = False
        self.connect("closed", self._closed)
        self.connect("close-attempt", lambda *_: None)
        self.set_can_close(True)

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Meter")
        self.meter_row = Adw.ActionRow(title="Bootloader now", subtitle="asking the meter…", subtitle_lines=2)
        self.fw_row = Adw.ActionRow(title="Firmware", subtitle="—")
        self.bat_row = Adw.ActionRow(title="Battery", subtitle="—")
        for r in (self.meter_row, self.fw_row, self.bat_row):
            g.add(r)
        page.add(g)

        g = Adw.PreferencesGroup(title="zombu4's bootloader release")
        self.rel_row = Adw.ActionRow(title="Latest", subtitle="checking GitHub…", subtitle_lines=3)
        g.add(self.rel_row)
        page.add(g)

        self.steps = Adw.PreferencesGroup(title="What happens")
        page.add(self.steps)

        pg = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        self.progress = Gtk.ProgressBar(show_text=True, text="")
        self.status = Gtk.Label(wrap=True, xalign=0, label="", css_classes=["dim-label"])
        box.append(self.progress)
        box.append(self.status)
        frame = Gtk.Frame(child=box, css_classes=["card"])
        pg.add(frame)
        page.add(pg)

        bg = Adw.PreferencesGroup()
        btns = Gtk.Box(spacing=12, halign=Gtk.Align.END)
        self.go_btn = Gtk.Button(label="Install", sensitive=False, css_classes=["suggested-action"])
        self.go_btn.connect("clicked", lambda *_: self._confirm())
        btns.append(self.go_btn)
        bg.add(btns)
        page.add(bg)

        tv.set_content(page)
        self.set_child(tv)
        self._poll = GLib.timeout_add(500, self._refresh)
        self._fetch()
        self._refresh()

    # ---- release ----------------------------------------------------------------

    def _fetch(self) -> None:
        def work():
            try:
                rel = releases.fetch_latest_bootloader()
                files = releases.download_bootloader(rel, data_dir() / "bootloader") if rel else None
                GLib.idle_add(self._fetched, rel, files, None)
            except Exception as e:                       # offline, rate limit, a failed check
                GLib.idle_add(self._fetched, None, None, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, rel, files, err) -> bool:
        if err or rel is None:
            self.rel_row.set_subtitle(f"Could not get it: {err or 'no bootloader release found'}")
        else:
            self.release, self.files = rel, files
            inst, boot = files
            self.rel_row.set_title(f"Latest: {rel.meter_text} ({rel.tag})")
            self.rel_row.set_subtitle(
                f"blimage.bin {len(boot):,} bytes, install.bin {len(inst):,} bytes · zip checked against its "
                f"published SHA-256 and every file against SHA256SUMS · blimage {hashlib.sha256(boot).hexdigest()[:12]}…")
        self._refresh()
        return GLib.SOURCE_REMOVE

    # ---- state ------------------------------------------------------------------

    def _route(self) -> str:
        """'update', 'first', 'old-firmware', 'asking', 'offline' or 'current'."""
        link = self.link
        if not link.is_connected:
            return "offline"
        bl = link.update_mode or link.bootloader
        if bl == "B-EEV":
            return "first"
        if releases.bootloader_key(bl):
            return "update" if self.release is None or releases.bootloader_is_newer(self.release, bl) else "current"
        fw = link.firmware_version or ""
        if fw and fw[:1] in "FC" and fw[2:] < "1.04":
            return "old-firmware"
        return "asking"

    def _refresh(self) -> bool:
        link = self.link
        bl = link.update_mode or link.bootloader
        self.meter_row.set_subtitle(
            "not connected" if not link.is_connected else
            "EEVblog's original (B-EEV)" if bl == "B-EEV" else
            f"zombu4's two-part bootloader {bl}" + (" · in update mode (-IAP-)" if link.update_mode else "") if bl else
            "not reported: this firmware does not answer the bootloader request" if self._route() == "old-firmware"
            else "asking the meter… (firmware v1.0.4 / C-1.04 and later answer)")
        self.fw_row.set_subtitle(link.firmware_version or ("update mode: no firmware running" if link.update_mode
                                                            else "unknown"))
        v = link.battery_voltage
        self.bat_row.set_subtitle("unknown" if v is None else
                                  f"{v:.2f} V" + ("" if v >= BATTERY_MIN else
                                                  f" · below {BATTERY_MIN:.2f} V: the meter refuses a first install"))
        if self.busy:
            if self._verify_until:
                self._check_verified()
            return GLib.SOURCE_CONTINUE
        route = self._route()
        rel = self.release
        new = rel.meter_text if rel else "the latest"
        texts = {
            "offline": "Connect to the meter first (Bluetooth on: hold PEAK).",
            "asking": "Waiting for the meter to report its bootloader. The firmware must be v1.0.4 / C-1.04 or later.",
            "old-firmware": f"The meter runs {link.firmware_version}: install firmware v1.0.4 / C-1.04 or later first "
                            "(Upload Firmware…). Older firmware cannot install or report the bootloader.",
            "current": f"The meter already has {bl}, the latest release. Nothing to do.",
            "update": (f"Update {bl} → {new}.\n"
                       "1. Stay on a measuring page, Bluetooth on. Fresh batteries are best.\n"
                       "2. Press Update: the meter restarts into its update mode (-IAP-), receives the bootloader "
                       "(about 20 s) into a spare slot and restarts. Its starter copies the new bootloader in (display "
                       "blank for about a second), then the firmware starts.\n"
                       "The running bootloader is only replaced after the whole image arrived and was checked. "
                       "If the display stays blank afterwards, take the batteries out and put them back: the starter "
                       "puts the previous bootloader back."),
            "first": (f"First install of zombu4's bootloader {new} (the meter has EEVblog's).\n"
                      f"1. Fresh batteries: the meter refuses below {BATTERY_MIN:.2f} V (LdE 8).\n"
                      "2. Bluetooth on, logging off, then SETUP and REL / PEAK to the page Ld: the meter shows Ld-bt.\n"
                      "3. Press Install: the meter receives install.bin (about 30 s, Ld nn), checks that its "
                      "bootloader is one of EEVblog's (LdE 7 if not), writes the bootloader and restarts.\n"
                      "Don't switch the meter off or take the batteries out until it shows a reading again: for "
                      "about 10 ms of the write a power cut would leave it without a start (zombu4's README). "
                      "Afterwards firmware updates and later bootloader updates go over Bluetooth, no card, no keys."),
        }
        self.steps.set_description(texts.get(route, ""))
        self._first_route = route == "first"
        self.link.upload_mode = route == "first" and self.link.on_ld_page
        ready = self.files is not None and route in ("update", "first")
        if route == "first":
            v = link.battery_voltage
            ready = ready and link.on_ld_page and (v is None or v >= BATTERY_MIN)
            if self.files is not None and not link.on_ld_page:
                self.status.set_label("Put the meter on the SETUP page Ld (Ld-bt) to install.")
        self.go_btn.set_label("Update" if route == "update" else "Install")
        self.go_btn.set_sensitive(ready)
        return GLib.SOURCE_CONTINUE

    # ---- install ----------------------------------------------------------------

    def _confirm(self) -> None:
        route = self._route()
        rel = self.release
        if route == "update":
            body = (f"The meter restarts into its update mode and receives bootloader {rel.meter_text}. Keep it on "
                    "and in range until it shows a reading again. Calibration and settings are not touched.")
        else:
            body = (f"The meter writes zombu4's bootloader {rel.meter_text} over EEVblog's. Keep it on, with fresh "
                    "batteries, until it shows a reading again. Calibration and settings are not touched. "
                    "Flashing is at your own risk (zombu4's LICENSE).")
        dlg = Adw.AlertDialog(heading="Install the bootloader?", body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", "Update" if route == "update" else "Install")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: r == "go" and self._start(route))
        dlg.present(self)

    def _start(self, route: str) -> None:
        inst, boot = self.files
        rel = self.release
        self.busy, self._all_sent, self._verify_until = True, False, 0.0
        self._target = rel.meter_text
        self.go_btn.set_sensitive(False)
        self.set_can_close(False)
        self._t0 = time.monotonic()
        if route == "update":
            self.status.set_label("Restarting the meter into its update mode…")
            self.link.install_firmware(boot, self._progress, self._done, self._state,
                                       kind=KIND_BOOTLOADER, version=rel.header_version)
        else:
            self.link.set_auto_reconnect(True)          # the meter restarts after the install: connect again
            self.status.set_label("Sending install.bin…")
            self.link.upload_firmware(inst, self._progress, self._done, kind=KIND_INSTALL)

    def _state(self, text: str) -> None:
        self.status.set_label(text.replace("the firmware", "the bootloader image"))

    def _progress(self, fraction: float, sent: int, total: int, bps: float) -> None:
        self.progress.set_fraction(fraction)
        self.progress.set_text(f"{sent:,} / {total:,} bytes · {bps:.0f} B/s")
        if fraction >= 1.0:
            self._all_sent = True
            self.status.set_label("All sent: the meter checks and writes it…")

    def _done(self, ok: bool, text: str) -> None:
        if ok or (self._first_route and self._all_sent):
            # the meter restarts: its own report after the restart decides
            self.status.set_label("The meter restarts and installs the bootloader. Waiting for it to report "
                                  f"{self._target}…")
            self._verify_until = time.monotonic() + VERIFY_S
            return
        self._finish(False, text)

    def _check_verified(self) -> None:
        bl = self.link.bootloader
        if bl == self._target and self.link.firmware_version:
            self._finish(True, f"Done: the meter reports bootloader {bl} and runs {self.link.firmware_version}.")
        elif time.monotonic() > self._verify_until:
            self._finish(False, f"The meter did not report {self._target} within {VERIFY_S} s (it says "
                                f"{bl or 'nothing yet'}). If its display stays blank, take the batteries out and put "
                                "them back. If it shows -IAP-, send the firmware again (Upload Firmware…).")

    def _finish(self, ok: bool, text: str) -> None:
        self.busy = False
        self._verify_until = 0.0
        self.set_can_close(True)
        self.link.set_auto_reconnect(bool(self.win.settings.auto_reconnect))
        self.status.set_label(text)
        self.progress.set_text("done" if ok else "stopped")
        if ok:
            self.progress.set_fraction(1.0)
            self.win.toast(text)
        self._refresh()

    def _closed(self, *_a) -> None:
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0
        self.link.upload_mode = False
