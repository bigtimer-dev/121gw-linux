"""Meter link state shared by the BlueZ transport and the demo meter."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from gi.repository import GLib

from .keys import VERSION_REQUEST, MeterKey, clock_applies_every_time, clock_payload, parse_version_answer
from .packet import FrameAssembler, PacketV2
from .upload import AnswerParser, UploadSession

COMM_TIMEOUT = 5.0
BATTERY_LOW_V = 4.5
BATTERY_POWER_OFF_V = 4.2
CLOCK_REPLY_MS = 3000   # the meter echoes a set-clock frame within a few frames


@dataclass
class DiscoveredDevice:
    address: str
    name: str
    rssi: int | None = None
    is_meter: bool = True
    connected: bool = False


class MeterLink:
    """Base class: owns live meter state and notifies listeners on the GLib main loop."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.devices: dict[str, DiscoveredDevice] = {}
        self.is_scanning = False
        self.is_connected = False
        self.connecting = False
        self.adapter_off = False
        self.status_text = "Ready"
        self.latest: PacketV2 | None = None
        self.rssi: int | None = None
        self.packets_received = 0
        self.battery_voltage: float | None = None
        self.meter_time: str | None = None
        self.communication_lost = False
        self.in_setup_menu = False
        self.connected_address: str | None = None
        self.connected_name: str | None = None
        self._last_packet_at: float | None = None
        self._assembler = FrameAssembler()
        self._watchdog_id = 0
        self._packet_listeners: list[Callable[[PacketV2], None]] = []
        self._change_listeners: list[Callable[[], None]] = []
        self._change_pending = False
        self._clock_frame: bytes | None = None
        self._clock_when: datetime | None = None
        self._clock_cb: Callable[[str], None] | None = None
        self._clock_tail = bytearray()
        self._clock_timer = 0
        self._clock_repeat = False
        self._clock_synced = False      # a clock frame was echoed on this connection
        self.firmware_version: str | None = None    # open firmware v1.0.1+ answers the version request
        self._version_tail = bytearray()
        self._version_asks = 0
        self.upload_mode = False        # the upload dialog is open: the meter's page Ld sends no reading frames
        self._upload: UploadSession | None = None
        self._upload_parser = AnswerParser()
        self._upload_timer = 0
        self._upload_t0 = 0.0
        self._upload_progress: Callable[[float, int, int, float], None] | None = None
        self._upload_done: Callable[[bool, str], None] | None = None

    # ---- observers ---------------------------------------------------------

    def connect_packet(self, fn: Callable[[PacketV2], None]) -> None:
        self._packet_listeners.append(fn)

    def connect_changed(self, fn: Callable[[], None]) -> None:
        self._change_listeners.append(fn)

    def changed(self) -> None:
        """Coalesce state-change notifications into one idle callback."""
        if self._change_pending:
            return
        self._change_pending = True

        def fire():
            self._change_pending = False
            for fn in list(self._change_listeners):
                fn()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(fire)

    def set_status(self, text: str) -> None:
        if text != self.status_text:
            self.status_text = text
            self.changed()

    # ---- derived state -----------------------------------------------------

    @property
    def battery_low(self) -> bool:
        if self.battery_voltage is not None:
            return self.battery_voltage <= BATTERY_LOW_V
        return bool(self.latest and self.latest.icon_bat)

    @property
    def battery_near_power_off(self) -> bool:
        return self.battery_voltage is not None and self.battery_voltage <= BATTERY_POWER_OFF_V

    # ---- packet handling ---------------------------------------------------

    def _start_watchdog(self) -> None:
        self._stop_watchdog()
        self._watchdog_id = GLib.timeout_add_seconds(1, self._watchdog_tick)

    def _stop_watchdog(self) -> None:
        if self._watchdog_id:
            GLib.source_remove(self._watchdog_id)
            self._watchdog_id = 0

    def _watchdog_tick(self) -> bool:
        if not self.is_connected:
            lost = True
        elif self.upload_mode:
            lost = False                # page Ld: no reading frames by design
        else:
            lost = self._last_packet_at is not None and time.monotonic() - self._last_packet_at > COMM_TIMEOUT
        if lost != self.communication_lost:
            self.communication_lost = lost
            self.changed()
        return GLib.SOURCE_CONTINUE

    def ingest(self, data: bytes) -> None:
        if self._upload is not None:
            for a in self._upload_parser.feed(data):
                self._upload_answer(*a)
        if self._clock_frame is not None:
            self._match_clock_echo(data)
        if self.firmware_version is None and self._version_asks:
            self._match_version(data)
        packets = self._assembler.feed(data)
        if packets:
            self._publish(packets[-1])

    def _publish(self, pkt: PacketV2) -> None:
        self.latest = pkt
        self._last_packet_at = time.monotonic()
        self.communication_lost = False
        self.packets_received += 1
        if pkt.sub_mode_raw == 110 and math.isfinite(pkt.sub_value):
            self.battery_voltage = abs(pkt.sub_value)
        elif pkt.sub_mode_raw == 140:
            n = abs(pkt.sub_int_value)
            self.meter_time = f"{min(23, (n >> 8) & 0xFF):02d}:{min(59, n & 0xFF):02d}"
        if self.firmware_version is None and self.packets_received in (2, 12) and self._version_asks < 2:
            self._version_asks += 1                 # ask once the link streams; once more if the answer was lost
            self.write_frame(VERSION_REQUEST, None)
        for fn in list(self._packet_listeners):
            fn(pkt)
        self.changed()

    def _match_version(self, data: bytes) -> None:
        tail = self._version_tail
        tail += data
        v = parse_version_answer(bytes(tail))
        if v:
            self.firmware_version = v
            tail.clear()
            self.changed()
        elif len(tail) > 64:
            del tail[:-6]

    def _reset_link_state(self) -> None:
        self._stop_watchdog()
        self._assembler.clear()
        self.is_connected = False
        self.connecting = False
        self.in_setup_menu = False
        self._last_packet_at = None
        self._clock_finish("Clock update cancelled: disconnected")
        self._clock_synced = False
        self.firmware_version = None
        self._version_tail.clear()
        self._version_asks = 0
        self._upload_finish(False, "Upload stopped: disconnected")

    def note_key(self, key: MeterKey) -> None:
        """Track whether the meter is in its SETUP menu (REL/PEAK act as arrows there)."""
        if key is MeterKey.SETUP:
            self.in_setup_menu = True
        elif key not in (MeterKey.REL, MeterKey.PEAK):
            self.in_setup_menu = False

    def key_status(self, key: MeterKey, long_press: bool) -> str:
        if long_press and key is MeterKey.MODE:
            return "Backlight"
        return f"Hold {key.title}" if long_press else f"Sent {key.title}"

    # ---- meter clock -------------------------------------------------------

    def sync_clock(self, on_result: Callable[[str], None]) -> None:
        """Set the meter's clock to this computer's local time; on_result gets one line for the user."""
        if not self.is_connected:
            on_result("Not connected")
            return
        self._clock_finish(None)
        when = datetime.now()
        self._clock_frame = clock_payload(when)
        self._clock_when = when
        self._clock_cb = on_result
        self._clock_repeat = self._clock_synced and not clock_applies_every_time(self.firmware_version)
        self._clock_tail.clear()
        self._clock_timer = GLib.timeout_add(CLOCK_REPLY_MS, self._clock_timed_out)
        self.write_frame(self._clock_frame, self._clock_written)

    def _clock_written(self, error: str | None) -> None:
        if error:
            self._clock_finish(f"Clock not sent: {error}")

    def _match_clock_echo(self, data: bytes) -> None:
        frame, tail = self._clock_frame, self._clock_tail
        tail += data
        if frame in tail:
            self._clock_synced = True
            when = self._clock_when
            if self._clock_repeat:
                text = ("Already synced: the meter takes one clock update each time its Bluetooth is switched on "
                        "(hold PEAK)")
            else:
                text = f"Meter clock set to {when:%H:%M:%S}, {when:%Y-%m-%d}"
            self._clock_finish(text)
        elif len(tail) >= len(frame):
            del tail[: len(tail) - len(frame) + 1]

    def _clock_timed_out(self) -> bool:
        self._clock_timer = 0
        self._clock_finish("The meter did not confirm the clock update")
        return GLib.SOURCE_REMOVE

    def _clock_finish(self, text: str | None) -> None:
        if self._clock_timer:
            GLib.source_remove(self._clock_timer)
            self._clock_timer = 0
        cb = self._clock_cb
        self._clock_frame = self._clock_cb = None
        self._clock_tail.clear()
        if cb is not None and text:
            cb(text)

    # ---- firmware upload (open firmware v1.0.0+, SETUP page Ld) ------------

    @property
    def uploading(self) -> bool:
        return self._upload is not None

    def upload_firmware(self, data: bytes, on_progress: Callable[[float, int, int, float], None],
                        on_done: Callable[[bool, str], None]) -> None:
        """Send a firmware file to the meter's SD card; on_progress(fraction, sent, total, bytes/s), on_done(ok, text)."""
        if not self.is_connected:
            on_done(False, "Not connected")
            return
        self.cancel_upload()
        self._upload = UploadSession(data)
        self._upload_parser.clear()
        self._upload_progress, self._upload_done = on_progress, on_done
        self._upload_t0 = time.monotonic()
        self._upload_send()

    def cancel_upload(self) -> None:
        if self._upload is not None:
            abort = self._upload.abort_packet()
            self._upload_finish(False, "Upload cancelled")
            self.write_frame(abort, None)           # the meter goes back to waiting (Ld-bt)

    def _upload_send(self) -> None:
        up = self._upload
        if up is None:
            return
        step = up.step()
        self._upload_stop_timer()
        self._upload_timer = GLib.timeout_add(step.timeout_ms, self._upload_timed_out)
        self.write_frame(step.packet, self._upload_written)

    def _upload_written(self, error: str | None) -> None:
        if error and self._upload is not None:
            self._upload_finish(False, f"Bluetooth write failed: {error}")

    def _upload_answer(self, seq: int, status: int, nxt: int) -> None:
        up = self._upload
        if up is None:
            return
        advanced = up.answer(seq, status, nxt)
        if up.done:
            self._upload_finish(up.ok, up.message)
        elif advanced:
            if self._upload_progress:
                elapsed = max(time.monotonic() - self._upload_t0, 1e-6)
                self._upload_progress(up.fraction, up.sent_bytes, len(up.data), up.sent_bytes / elapsed)
            self._upload_send()

    def _upload_timed_out(self) -> bool:
        self._upload_timer = 0
        up = self._upload
        if up is not None:
            if up.timeout():
                self._upload_send()
            else:
                self._upload_finish(False, up.message)
        return GLib.SOURCE_REMOVE

    def _upload_stop_timer(self) -> None:
        if self._upload_timer:
            GLib.source_remove(self._upload_timer)
            self._upload_timer = 0

    def _upload_finish(self, ok: bool, text: str) -> None:
        self._upload_stop_timer()
        if self._upload is None:
            return
        cb = self._upload_done
        self._upload = None
        self._upload_progress = self._upload_done = None
        self._upload_parser.clear()
        if cb is not None:
            cb(ok, text)
        self.changed()

    # ---- transport API (overridden) ----------------------------------------

    def start(self) -> None: ...
    def start_scan(self) -> None: ...
    def stop_scan(self) -> None: ...
    def connect_device(self, address: str) -> None: ...
    def power_on(self) -> None: ...
    def disconnect(self) -> None: ...
    def send_key(self, key: MeterKey, long_press: bool = False) -> None: ...
    def write_frame(self, payload: bytes, done: Callable[[str | None], None] | None = None) -> None: ...
    def set_auto_reconnect(self, value: bool) -> None: ...
    def shutdown(self) -> None: ...
