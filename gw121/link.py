"""Meter link state shared by the BlueZ transport and the demo meter."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib

from .keys import MeterKey
from .packet import FrameAssembler, PacketV2

COMM_TIMEOUT = 5.0
BATTERY_LOW_V = 4.5
BATTERY_POWER_OFF_V = 4.2


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
        else:
            lost = self._last_packet_at is not None and time.monotonic() - self._last_packet_at > COMM_TIMEOUT
        if lost != self.communication_lost:
            self.communication_lost = lost
            self.changed()
        return GLib.SOURCE_CONTINUE

    def ingest(self, data: bytes) -> None:
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
        for fn in list(self._packet_listeners):
            fn(pkt)
        self.changed()

    def _reset_link_state(self) -> None:
        self._stop_watchdog()
        self._assembler.clear()
        self.is_connected = False
        self.connecting = False
        self.in_setup_menu = False
        self._last_packet_at = None

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

    # ---- transport API (overridden) ----------------------------------------

    def start(self) -> None: ...
    def start_scan(self) -> None: ...
    def stop_scan(self) -> None: ...
    def connect_device(self, address: str) -> None: ...
    def power_on(self) -> None: ...
    def disconnect(self) -> None: ...
    def send_key(self, key: MeterKey, long_press: bool = False) -> None: ...
    def set_auto_reconnect(self, value: bool) -> None: ...
    def shutdown(self) -> None: ...
