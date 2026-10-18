"""121GW transport over BlueZ's D-Bus API (no third-party BLE library needed).

The meter uses the Bluegiga BLE112 "Cable Replacement" profile:
  service        0bd51666-e7cb-469b-8e4d-2742f1ba77cc
  characteristic e7add780-b042-4876-aae1-112855353cc1  (indicate + write)
Frames arrive as GATT indications; BlueZ's StartNotify enables them.
"""

from __future__ import annotations

from gi.repository import Gio, GLib

from .keys import MeterKey
from .link import DiscoveredDevice, MeterLink

BLUEZ = "org.bluez"
IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_SERVICE = "org.bluez.GattService1"
IFACE_CHAR = "org.bluez.GattCharacteristic1"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
IFACE_OM = "org.freedesktop.DBus.ObjectManager"

CHAR_UUID = "e7add780-b042-4876-aae1-112855353cc1"
CABLE_SERVICE = "0bd51666-e7cb-469b-8e4d-2742f1ba77cc"
_STANDARD_SERVICES = ("00001800", "00001801", "0000180a", "0000180f")

CONNECT_TIMEOUT_MS = 30_000
RESOLVE_TIMEOUT_S = 20
RETRY_DELAY_S = 3


def _name(props: dict) -> str:
    return props.get("Name") or props.get("Alias") or props.get("Address", "?")


def _is_meter(props: dict) -> bool:
    if "121GW" in (props.get("Name") or props.get("Alias") or "").upper():
        return True
    return CABLE_SERVICE in [u.lower() for u in props.get("UUIDs", [])]


class BluezLink(MeterLink):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.bus: Gio.DBusConnection | None = None
        self.objects: dict[str, dict[str, dict]] = {}
        self.adapter: str | None = None
        self.device_path: str | None = None
        self.notify_char: str | None = None
        self.write_char: str | None = None
        self.write_type = "request"
        self._subs: list[int] = []
        self._resolve_timeout_id = 0
        self._retry_id = 0
        self._scan_wanted = False
        self._link_preexisting = False   # BlueZ already had the meter connected: leave it that way on quit

    # ---- setup -------------------------------------------------------------

    def start(self) -> None:
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            self.set_status(f"D-Bus unavailable: {e.message}")
            return
        for iface, member in ((IFACE_OM, "InterfacesAdded"), (IFACE_OM, "InterfacesRemoved"),
                              (IFACE_PROPS, "PropertiesChanged")):
            self._subs.append(self.bus.signal_subscribe(
                BLUEZ, iface, member, None, None, Gio.DBusSignalFlags.NONE, self._on_signal))
        try:
            reply = self.bus.call_sync(BLUEZ, "/", IFACE_OM, "GetManagedObjects", None,
                                       GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error as e:
            self.set_status("BlueZ not running" if "ServiceUnknown" in e.message else e.message)
            return
        self.objects = reply.unpack()[0]
        self._pick_adapter()
        for path, ifaces in self.objects.items():
            if IFACE_DEVICE in ifaces:
                self._update_device(path, ifaces[IFACE_DEVICE])
        if self.adapter is None or self.adapter_off:
            return
        if self.settings.auto_reconnect:
            self._watch_for_meter()
        else:
            self.set_status("Bluetooth ready")

    def _pick_adapter(self) -> None:
        adapters = sorted(p for p, i in self.objects.items() if IFACE_ADAPTER in i)
        if not adapters:
            self.adapter = None
            self.set_status("No Bluetooth adapter found")
            return
        self.adapter = adapters[0]
        self._update_power()

    def _update_power(self) -> None:
        props = self.objects.get(self.adapter, {}).get(IFACE_ADAPTER, {})
        self.adapter_off = not props.get("Powered", False)
        if self.adapter_off:
            blocked = "blocked" in str(props.get("PowerState", ""))
            self.set_status("Bluetooth is blocked (rfkill)" if blocked else "Bluetooth is off")
        self.changed()

    def power_on(self) -> None:
        if not self.adapter:
            return

        def done(_res, err):
            if err:
                msg = err.message.split(":")[-1].strip()
                self.set_status("Bluetooth is blocked – run: rfkill unblock bluetooth"
                                if "Blocked" in err.message or "rfkill" in err.message.lower()
                                else f"Could not turn on Bluetooth: {msg}")

        self._set_prop(self.adapter, IFACE_ADAPTER, "Powered", GLib.Variant("b", True), done)

    def shutdown(self) -> None:
        self._stop_watchdog()
        if self.is_scanning:
            self.stop_scan()
        if self.bus:
            for s in self._subs:
                self.bus.signal_unsubscribe(s)
            self._subs.clear()
            if self.notify_char:
                self._call(self.notify_char, IFACE_CHAR, "StopNotify", None)
            if self.device_path and self.is_connected and not self._link_preexisting:
                self._call(self.device_path, IFACE_DEVICE, "Disconnect", None)
            self.bus.flush_sync(None)

    # ---- D-Bus helpers -----------------------------------------------------

    def _call(self, path, iface, method, params, cb=None, timeout=10_000) -> None:
        if not self.bus:
            return

        def done(bus, res):
            try:
                result = bus.call_finish(res)
                err = None
            except GLib.Error as e:
                result, err = None, e
            if cb:
                cb(result, err)

        self.bus.call(BLUEZ, path, iface, method, params, None, Gio.DBusCallFlags.NONE, timeout, None, done)

    def _set_prop(self, path, iface, name, value, cb=None) -> None:
        self._call(path, IFACE_PROPS, "Set", GLib.Variant("(ssv)", (iface, name, value)), cb)

    # ---- signals -----------------------------------------------------------

    def _on_signal(self, _conn, _sender, path, iface, member, params) -> None:
        if member == "InterfacesAdded":
            obj_path, ifaces = params.unpack()
            self.objects.setdefault(obj_path, {}).update(ifaces)
            if IFACE_ADAPTER in ifaces and self.adapter is None:
                self._pick_adapter()
                if self.adapter and self.settings.auto_reconnect:
                    self._watch_for_meter()
            if IFACE_DEVICE in ifaces:
                self._update_device(obj_path, ifaces[IFACE_DEVICE], seen=True)
            if IFACE_CHAR in ifaces and self._awaiting_gatt():
                self._maybe_setup_gatt()
        elif member == "InterfacesRemoved":
            obj_path, names = params.unpack()
            obj = self.objects.get(obj_path, {})
            for n in names:
                obj.pop(n, None)
            if not obj:
                self.objects.pop(obj_path, None)
            if IFACE_DEVICE in names:
                addr = next((a for a, d in self.devices.items() if self._path_for(a) == obj_path), None)
                if addr and addr != self.connected_address:
                    self.devices.pop(addr, None)
                    self.changed()
            if obj_path == self.adapter:
                self.adapter = None
                self._handle_lost()
                self._pick_adapter()
        elif member == "PropertiesChanged":
            iface_name, changed, invalidated = params.unpack()
            props = self.objects.setdefault(path, {}).setdefault(iface_name, {})
            props.update(changed)
            for k in invalidated:
                props.pop(k, None)
            if iface_name == IFACE_CHAR and path == self.notify_char and "Value" in changed:
                self.ingest(bytes(changed["Value"]))
            elif iface_name == IFACE_DEVICE:
                self._on_device_props(path, changed)
            elif iface_name == IFACE_ADAPTER and path == self.adapter:
                if "Discovering" in changed:
                    self.is_scanning = bool(changed["Discovering"])
                    self.changed()
                if "Powered" in changed or "PowerState" in changed:
                    was_off = self.adapter_off
                    if changed.get("Powered") is False:
                        self._handle_lost()
                        self.is_scanning = False
                    self._update_power()
                    if was_off and not self.adapter_off:
                        self.set_status("Bluetooth ready")
                        if self._scan_wanted:
                            self.start_scan()
                        elif self.settings.auto_reconnect:
                            self._watch_for_meter()

    def _on_device_props(self, path: str, changed: dict) -> None:
        props = self.objects.get(path, {}).get(IFACE_DEVICE, {})
        self._update_device(path, props, seen=bool({"RSSI", "ManufacturerData"} & changed.keys())
                            or changed.get("Connected") is True)
        if path != self.device_path:
            return
        if changed.get("Connected") is False and (self.is_connected or self.connecting):
            self._handle_lost()
        if changed.get("ServicesResolved") is True and self._awaiting_gatt():
            self._maybe_setup_gatt()

    # ---- device list -------------------------------------------------------

    def _path_for(self, address: str) -> str | None:
        if not self.adapter:
            return None
        return f"{self.adapter}/dev_{address.replace(':', '_')}"

    def _update_device(self, path: str, props: dict, seen: bool = False) -> None:
        if not self.adapter or not path.startswith(self.adapter + "/"):
            return
        addr = props.get("Address")
        if not addr:
            return
        meter = _is_meter(props)
        visible = meter or (self.settings.show_all_devices and ("RSSI" in props or seen))
        if not visible and addr not in self.devices:
            return
        dev = self.devices.get(addr)
        if dev is None:
            dev = self.devices[addr] = DiscoveredDevice(addr, _name(props))
        dev.name = _name(props)
        dev.is_meter = meter
        dev.connected = bool(props.get("Connected"))
        if "RSSI" in props:
            dev.rssi = int(props["RSSI"])
            if addr == self.connected_address or (meter and not self.is_connected):
                self.rssi = dev.rssi
        self.changed()
        if seen and meter:
            self._maybe_auto_connect(addr)

    def _maybe_auto_connect(self, address: str) -> None:
        if not self.settings.auto_reconnect or self.is_connected or self.connecting:
            return
        last = self.settings.last_device
        meters = [d for d in self.devices.values() if d.is_meter]
        if last is None or last == address or len(meters) == 1:
            self.connect_device(address)

    # ---- scanning ----------------------------------------------------------

    def start_scan(self) -> None:
        if self.is_connected or not self.adapter:
            if not self.adapter:
                self.set_status("No Bluetooth adapter found")
            return
        if self.adapter_off:
            self._scan_wanted = True
            self._update_power()
            return
        self._scan_wanted = False
        if not self.is_connected:
            self.set_status("Scanning for 121GW…")
        flt = GLib.Variant("(a{sv})", ({"Transport": GLib.Variant("s", "le"),
                                         "DuplicateData": GLib.Variant("b", True)},))

        def started(_res, err):
            if err and "InProgress" not in err.message:
                self.is_scanning = False
                self.set_status(f"Scan failed: {err.message.split(':')[-1].strip()}")
            else:
                self.is_scanning = True
                self.changed()

        self._call(self.adapter, IFACE_ADAPTER, "SetDiscoveryFilter", flt,
                   lambda _r, _e: self._call(self.adapter, IFACE_ADAPTER, "StartDiscovery", None, started))

    def stop_scan(self) -> None:
        if not self.adapter:
            return
        self._scan_wanted = False
        self._call(self.adapter, IFACE_ADAPTER, "StopDiscovery", None)
        self.is_scanning = False
        if not self.is_connected and not self.connecting:
            self.set_status("Scan stopped")
        self.changed()

    # ---- connection --------------------------------------------------------

    def connect_device(self, address: str) -> None:
        if self.connecting or not self.adapter:
            return
        if self.is_connected and address == self.connected_address:
            return
        if self.is_connected:
            self._teardown(disconnect=True)
        path = self._path_for(address)
        if path not in self.objects:
            self.set_status("Device not known to BlueZ – scan first")
            return
        self._cancel_retry()
        if self.is_scanning:
            self.stop_scan()
        self.connecting = True
        self.device_path = path
        self.connected_address = address
        self.connected_name = self.devices.get(address, DiscoveredDevice(address, "121GW")).name
        self.settings.last_device = address
        self._link_preexisting = bool(self.objects[path].get(IFACE_DEVICE, {}).get("Connected"))
        self.set_status("Connecting…")
        self.changed()

        def done(_res, err):
            if self.device_path != path:
                return
            if err and "AlreadyConnected" in err.message:
                err = None
            if err:
                self.connecting = False
                self.set_status("Connect failed – retrying" if self.settings.auto_reconnect else
                                f"Connect failed: {err.message.split(':')[-1].strip()}")
                self._schedule_retry()
                return
            self.set_status("Discovering services…")
            props = self.objects.get(path, {}).get(IFACE_DEVICE, {})
            self._resolve_timeout_id = GLib.timeout_add_seconds(RESOLVE_TIMEOUT_S, self._resolve_timed_out)
            if props.get("ServicesResolved"):
                self._maybe_setup_gatt()

        self._call(path, IFACE_DEVICE, "Connect", None, done, timeout=CONNECT_TIMEOUT_MS)

    def _awaiting_gatt(self) -> bool:
        return self.connecting and self.device_path is not None and self.notify_char is None

    def _resolve_timed_out(self) -> bool:
        self._resolve_timeout_id = 0
        if self._awaiting_gatt():
            self.set_status("121GW data characteristic not found")
            self._teardown(disconnect=True)
            self._schedule_retry()
        return GLib.SOURCE_REMOVE

    def _maybe_setup_gatt(self) -> None:
        dev_props = self.objects.get(self.device_path, {}).get(IFACE_DEVICE, {})
        if not dev_props.get("ServicesResolved"):
            return
        prefix = self.device_path + "/"
        services = {p: i[IFACE_SERVICE].get("UUID", "").lower()
                    for p, i in self.objects.items() if p.startswith(prefix) and IFACE_SERVICE in i}
        chars = [(p, i[IFACE_CHAR]) for p, i in self.objects.items() if p.startswith(prefix) and IFACE_CHAR in i]
        if not chars:
            return

        def svc_uuid(char_props):
            return services.get(char_props.get("Service", ""), "")

        def can_listen(c):
            return bool({"notify", "indicate"} & set(c.get("Flags", [])))

        def can_write(c):
            return bool({"write", "write-without-response"} & set(c.get("Flags", [])))

        notify = write = None
        for p, c in chars:  # 1) the documented data characteristic
            if c.get("UUID", "").lower() == CHAR_UUID:
                if can_listen(c):
                    notify = (p, c)
                if can_write(c):
                    write = (p, c)
        # 2) fallbacks: Cable Replacement service first, then any non-standard service
        for p, c in sorted(chars, key=lambda pc: svc_uuid(pc[1]) != CABLE_SERVICE):
            if notify and write:
                break
            if svc_uuid(c).startswith(_STANDARD_SERVICES):
                continue
            if notify is None and can_listen(c):
                notify = (p, c)
            if write is None and can_write(c):
                write = (p, c)
        if notify is None:
            return  # keep waiting for more InterfacesAdded until the timeout
        self.notify_char = notify[0]
        if write:
            self.write_char = write[0]
            self.write_type = "request" if "write" in write[1].get("Flags", []) else "command"
        self.set_status("Enabling indications…")

        def notified(_res, err):
            if err and "InProgress" not in err.message and "Already" not in err.message:
                self.set_status(f"StartNotify failed: {err.message.split(':')[-1].strip()}")
                self._teardown(disconnect=True)
                self._schedule_retry()
                return
            if self._resolve_timeout_id:
                GLib.source_remove(self._resolve_timeout_id)
                self._resolve_timeout_id = 0
            self.connecting = False
            self.is_connected = True
            self.communication_lost = False
            self._last_packet_at = None
            self._assembler.clear()
            self.set_status("Live")
            self._start_watchdog()
            self.changed()

        self._call(self.notify_char, IFACE_CHAR, "StartNotify", None, notified)

    def _teardown(self, disconnect: bool) -> None:
        if self._resolve_timeout_id:
            GLib.source_remove(self._resolve_timeout_id)
            self._resolve_timeout_id = 0
        if disconnect and self.notify_char:
            self._call(self.notify_char, IFACE_CHAR, "StopNotify", None)
        if disconnect and self.device_path:
            self._call(self.device_path, IFACE_DEVICE, "Disconnect", None)
        self._reset_link_state()
        self.notify_char = self.write_char = None
        self.device_path = None
        self.changed()

    def _handle_lost(self) -> None:
        was = self.is_connected or self.connecting
        self._teardown(disconnect=False)
        self.communication_lost = was
        self.connected_address = None
        if self.settings.auto_reconnect and was:
            self.set_status("Lost – waiting for 121GW…")
            self._schedule_retry(0)
        elif was:
            self.set_status("Disconnected")

    def disconnect(self) -> None:
        self.set_auto_reconnect(False)
        self._cancel_retry()
        self._teardown(disconnect=True)
        self.connected_address = self.connected_name = None
        self.latest = None
        self.communication_lost = False
        self.set_status("Disconnected")
        self.changed()

    def set_auto_reconnect(self, value: bool) -> None:
        if self.settings.auto_reconnect == value:
            return
        self.settings.auto_reconnect = value
        if value and not self.is_connected:
            self._watch_for_meter()
        elif not value:
            self._cancel_retry()
        self.changed()

    def _schedule_retry(self, delay: int = RETRY_DELAY_S) -> None:
        if not self.settings.auto_reconnect:
            return
        self._cancel_retry()

        def fire():
            self._retry_id = 0
            self._watch_for_meter()
            return GLib.SOURCE_REMOVE

        self._retry_id = GLib.timeout_add_seconds(delay, fire) if delay else GLib.idle_add(fire)

    def _cancel_retry(self) -> None:
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0

    def _watch_for_meter(self) -> None:
        """Auto-reconnect: scan; connection starts as soon as the meter advertises."""
        if not self.settings.auto_reconnect or self.is_connected or self.connecting or not self.adapter:
            return
        if self.adapter_off:
            self._update_power()
            return
        # A meter BlueZ already holds a link to no longer advertises: attach to it directly.
        linked = [d.address for d in self.devices.values() if d.is_meter and d.connected]
        if linked:
            last = self.settings.last_device
            self.connect_device(last if last in linked else linked[0])
            return
        self.set_status("Waiting for 121GW…")
        self.start_scan()

    # ---- keys --------------------------------------------------------------

    def send_key(self, key: MeterKey, long_press: bool = False) -> None:
        if not self.is_connected:
            self.set_status("Not connected")
            return
        self.note_key(key)
        target = self.write_char or self.notify_char
        if not target:
            self.set_status("No write characteristic")
            return
        params = GLib.Variant("(aya{sv})", (key.payload(long_press), {"type": GLib.Variant("s", self.write_type)}))

        def done(_res, err):
            if err:
                self.set_status(f"Write failed: {err.message.split(':')[-1].strip()}")
            else:
                self.set_status(self.key_status(key, long_press))

        self._call(target, IFACE_CHAR, "WriteValue", params, done)
