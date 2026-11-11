"""Meter link state shared by the BlueZ transport and the demo meter."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from gi.repository import GLib

from .keys import (CLOCK_REQUEST, VERSION_REQUEST, MeterKey, clock_applies_every_time, clock_payload,
                   parse_clock_report, parse_version_answer)
from .packet import FrameAssembler, PacketV2
from .card import (custom_key, fast_types, TIMEOUT_MS, CardError, ReplyParser, StreamParser, repair_reply, request_packet,
                   stream_payload)
from .setup_pages import SetupText, parse_setup_frames
from .upload import (BOOTLOADER_INFO_REQUEST, PROBE_SEQ, T_DATA, UPDATE_MODE_REQUEST, AnswerParser, BootSession,
                     UploadSession, has_direct_update, image_version, parse_bootloader_answer, probe_packet,
                     probe_result, repair_answer)

COMM_TIMEOUT = 5.0
BATTERY_LOW_V = 4.5
BATTERY_POWER_OFF_V = 4.2
CLOCK_REPLY_MS = 3000   # the meter echoes a set-clock frame within a few frames
UPDATE_MODE_WAIT_S = 90  # 0x91 sent: the meter restarts, the link comes back, the bootloader answers the probe
UPDATE_MODE_REFUSED_S = 8  # readings still flowing this long after 0x91 without a drop: no zombu4 bootloader


@dataclass
class DiscoveredDevice:
    address: str
    name: str
    rssi: int | None = None
    is_meter: bool = True
    connected: bool = False


CARD_GAP_MS = 80                    # at least this long without a byte after part of a card answer (or 3 x the spacing of
                                    # its pieces): it arrived damaged


def _is_meter_frame(data: bytes) -> bool:
    """A whole reading frame (19 bytes, 0xF2) or a short answer of the key / version parser, not part of a card answer."""
    return (len(data) == 19 and data[0] == 0xF2) or (len(data) <= 11 and data[:1] in (b"\xF4", b"\xF6", b"\xF7",
                                                                                       b"\xFA"))


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
        self._conn_packets = 0          # readings on this connection: the version questions follow these
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
        self._key_wait: dict | None = None  # the last key frame until the meter echoes it (confirm_key)
        self._clock_report: dict | None = None  # a clock measurement request (measure_clock) until its FB answer
        self._quality: deque = deque()      # (time, readings, damaged) samples for link_quality
        self._clock_timer = 0
        self._clock_repeat = False
        self._clock_synced = False      # a clock frame was echoed on this connection
        self.firmware_version: str | None = None    # open firmware v1.0.1+ answers the version request
        self._version_tail = bytearray()
        self._version_asks = 0
        self.setup_text: SetupText | None = None    # custom firmware C-1.03+: the SETUP page shown (frame FA, F7 in C-1.03)
        self._setup_buf = bytearray()
        self._setup_at = 0.0
        self.upload_mode = False        # the upload dialog is open: the meter's page Ld sends no reading frames
        # zombu4's two-part bootloader: firmware v1.0.4+ answers 0x92 ("B-EEV" or "B-1.11"); in its update mode the
        # meter sends nothing but answers the probe with its version
        self.bootloader: str | None = None
        self._boot_tail = bytearray()
        self._boot_asks = 0
        self.update_mode: str | None = None
        self._boot: BootSession | None = None
        self._boot_job: dict | None = None      # data and callbacks of a direct update, from 0x91 to the END answer
        self._boot_tx: deque[tuple[int, bytes]] = deque()
        self._boot_writing = False
        self._boot_timer = 0
        self._boot_wait_timer = 0
        self._boot_parser = AnswerParser()
        self._boot_prev_auto: bool | None = None
        # page Ld detection: when the readings stop, ask with a STATUS packet; only the page Ld answers it
        self.on_ld_page = False
        self._connected_at: float | None = None
        self._probe_parser = AnswerParser()
        self._probe_n = 0
        self._probe_at = 0.0
        self._probe_answer_at = 0.0
        self._upload: UploadSession | None = None
        self._upload_parser = AnswerParser()
        self._answer_raw = bytearray()  # recent bytes during a transfer, for repair_answer
        self.answers_repaired = 0
        self._upload_timer = 0
        self._pieces_gen = 0            # bumped to drop the rest of a packet being written in pieces
        self._upload_t0 = 0.0
        self._upload_progress: Callable[[float, int, int, float], None] | None = None
        self._upload_done: Callable[[bool, str], None] | None = None
        # card files and settings over page Ld (open firmware v1.0.5+ / C-1.06+, gw121/card.py): one request at a time
        self.card_damaged = 0           # card answers that arrived damaged and were asked again
        self.card_busy = False          # a card operation runs: no Ld probes meanwhile (the meter is deaf on the card)
        self._card_seq = 0x4000
        self._card_wait: dict | None = None
        self._card_stream: dict | None = None      # a STREAM being received (card_stream)
        self._card_parser = ReplyParser()
        self._card_raw = bytearray()
        self._card_lock = threading.Lock()

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
        self._connected_at = time.monotonic()
        self._watchdog_id = GLib.timeout_add_seconds(1, self._watchdog_tick)

    def _stop_watchdog(self) -> None:
        if self._watchdog_id:
            GLib.source_remove(self._watchdog_id)
            self._watchdog_id = 0

    PROBE_AFTER = 2.0                   # seconds without readings before asking whether the meter is on page Ld
    PROBE_EVERY = 3.0
    PROBE_STALE = 7.0                   # no answer for this long: no longer on page Ld

    def _probe_ld(self) -> None:
        """Ask whether the silent meter is on the page Ld (open firmware v1.0.0+: only that page answers)."""
        now = time.monotonic()
        if not self.is_connected or self._upload is not None or self._boot is not None:
            return
        if self.card_busy:
            self._probe_answer_at = now             # the card answers stand in for the probe
            return
        waiting = self._boot_job is not None    # waiting for the update mode: ask at once and often
        quiet_since = self._last_packet_at if self._last_packet_at is not None else self._connected_at
        if quiet_since is None or now - quiet_since < (0.5 if waiting else self.PROBE_AFTER):
            return
        if (self.on_ld_page or self.update_mode) and now - self._probe_answer_at > self.PROBE_STALE:
            self.on_ld_page = False
            self.update_mode = None
            self.changed()
        if now - self._probe_at >= (1.0 if waiting else self.PROBE_EVERY):
            self._probe_at = now
            self._probe_n += 1
            self.write_frame(probe_packet(self._probe_n), None)

    def _watchdog_tick(self) -> bool:
        self._probe_ld()
        if not self.is_connected:
            lost = True
        elif self.upload_mode or self.on_ld_page or self.update_mode or self._boot_job is not None:
            lost = False                # page Ld, update mode: no reading frames by design
        else:
            lost = self._last_packet_at is not None and time.monotonic() - self._last_packet_at > COMM_TIMEOUT
        if lost != self.communication_lost:
            self.communication_lost = lost
            self.changed()
        return GLib.SOURCE_CONTINUE

    def ingest(self, data: bytes) -> None:
        box = self._card_stream
        if box is not None:
            for item in box["parser"].feed(data):
                if item[0] == "piece":
                    box["pieces"][item[1]] = item[2]
                elif item[1] == box.get("seq") and box["status"] is None:
                    box["status"], box["field"] = item[2], item[3]
            if not _is_meter_frame(data):
                box["last"] = time.monotonic()
        elif self._card_wait is not None:
            self._card_answers(data)
        if self._upload is not None:
            self._answers(self._upload, self._upload_parser, data, self._upload_answer)
        elif self._boot is not None:
            self._answers(self._boot, self._boot_parser, data, self._boot_answer)
        elif self._probe_at:
            for seq, status, nxt in self._probe_parser.feed(data):
                if seq & 0xFF00 == PROBE_SEQ:
                    self._probe_answered(status, nxt)
        if self.bootloader is None and self._boot_asks:
            self._match_bootloader(data)
        if self._clock_frame is not None:
            self._match_clock_echo(data)
        if self._key_wait is not None:
            self._match_key_echo(data)
        if self._clock_report is not None:
            self._match_clock_report(data)
        if self.firmware_version is None and self._version_asks:
            self._match_version(data)
        self._match_setup(data)
        packets = self._assembler.feed(data)
        if packets:
            self._publish(packets[-1])

    def _answers(self, session, parser: AnswerParser, data: bytes, handle) -> None:
        """The meter's answers to transfer packets; an answer with damaged first bytes is repaired when its CRC-16
        proves it (repair_answer)."""
        raw = self._answer_raw
        raw += data
        if len(raw) > 48:
            del raw[:-48]
        got = parser.feed(data)
        if got:
            raw.clear()
            for a in got:
                handle(*a)
            return
        fixed = repair_answer(bytes(raw), session.expected_seqs())
        if fixed is not None:
            raw.clear()
            self.answers_repaired += 1
            handle(*fixed)

    def _probe_answered(self, status: int, nxt: int) -> None:
        self._probe_answer_at = time.monotonic()
        boot = probe_result(status, nxt)
        ld = boot is None
        if ld != self.on_ld_page or boot != self.update_mode:
            self.on_ld_page, self.update_mode = ld, boot
            self.communication_lost = False
            self.changed()
        if boot and self._boot_job is not None and self._boot is None:
            if self._boot_job.get("after_end"):
                self._boot_finish(False, "The meter came back in its update mode (-IAP-): the bootloader did not "
                                         "accept the file. Send it again.")
            else:
                self._boot_begin()

    def _publish(self, pkt: PacketV2) -> None:
        if self.on_ld_page or self.update_mode or self._probe_at:
            self._probe_at = 0.0                    # readings again: the page Ld was left
            self.on_ld_page = False
            self.update_mode = None
            self._probe_parser.clear()
        job = self._boot_job
        if job is not None and self._boot is None and not job["dropped"] and \
                time.monotonic() - job["asked_at"] > UPDATE_MODE_REFUSED_S:
            self._boot_finish(False, "The meter did not restart into its update mode: it needs zombu4's two-part "
                                     "bootloader (bl-v1.1.0 or later). Use the SD card route instead.")
        self.latest = pkt
        self._last_packet_at = time.monotonic()
        self.communication_lost = False
        self.packets_received += 1
        self._conn_packets += 1
        if pkt.sub_mode_raw == 110 and math.isfinite(pkt.sub_value):
            self.battery_voltage = abs(pkt.sub_value)
        elif pkt.sub_mode_raw == 140:
            n = abs(pkt.sub_int_value)
            self.meter_time = f"{min(23, (n >> 8) & 0xFF):02d}:{min(59, n & 0xFF):02d}"
        if self.firmware_version is None and self._conn_packets in (2, 12) and self._version_asks < 2:
            self._version_asks += 1                 # ask once the link streams; once more if the answer was lost
            self.write_frame(VERSION_REQUEST, None)
        elif self.bootloader is None and self._asks_bootloader() and self._boot_asks < 2 and \
                self._conn_packets in (4, 16, 30):
            self._boot_asks += 1
            self.write_frame(BOOTLOADER_INFO_REQUEST, None)
        for fn in list(self._packet_listeners):
            fn(pkt)
        self.changed()

    SETUP_TEXT_TIMEOUT = 4.5            # the meter repeats the frame at least every 2 s while a page is shown

    def _match_setup(self, data: bytes) -> None:
        buf = self._setup_buf
        buf += data
        frames, done = parse_setup_frames(bytes(buf))
        del buf[:done]
        if len(buf) > 32:
            del buf[:-11]
        if frames:
            f = frames[-1]
            new = None if f.item == 0 else f
            self._setup_at = time.monotonic()
            if new != self.setup_text:
                self.setup_text = new
                self.changed()
        elif self.setup_text is not None and time.monotonic() - self._setup_at > self.SETUP_TEXT_TIMEOUT:
            self.setup_text = None
            self.changed()

    def _asks_bootloader(self) -> bool:
        """Open firmware v1.0.4+ answers the bootloader request (code 0x92); older builds only echo it."""
        v = self.firmware_version
        if not v or v[0] not in "FC":
            return False
        try:
            return float(v[2:]) >= 1.04
        except ValueError:
            return False

    def _match_bootloader(self, data: bytes) -> None:
        tail = self._boot_tail
        tail += data
        b = parse_bootloader_answer(bytes(tail))
        if b:
            self.bootloader = b
            tail.clear()
            self.changed()
        elif len(tail) > 64:
            del tail[:-6]

    @property
    def direct_update(self) -> bool:
        """The meter can take firmware straight into its flash (zombu4's bootloader), no SD card or keys."""
        return self.update_mode is not None or has_direct_update(self.bootloader)

    def _match_version(self, data: bytes) -> None:
        tail = self._version_tail
        tail += data
        v = parse_version_answer(bytes(tail))
        if v:
            self.firmware_version = v
            tail.clear()
            if self._boot_job is not None and self._boot_job.get("after_end"):
                self._boot_came_back(v)
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
        self._conn_packets = 0
        self.setup_text = None
        self._setup_buf.clear()
        self.on_ld_page = False
        self.update_mode = None
        self.bootloader = None
        self._boot_tail.clear()
        self._boot_asks = 0
        self._probe_at = self._probe_answer_at = 0.0
        self._probe_parser.clear()
        self._upload_finish(False, "Upload stopped: disconnected")
        if self._boot_job is not None:
            if self._boot is not None and self._boot.phase == "end":
                self._boot_after_end()              # every byte sent: the meter restarted before its END answer came
            elif self._boot is not None:
                self._boot_finish(False, "Disconnected during the transfer. The meter stays in its update mode "
                                         "(-IAP-): connect again and send the file again.")
            else:
                self._boot_job["dropped"] = True    # the restart into the update mode

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

    # ---- link quality --------------------------------------------------------

    QUALITY_S = 20.0

    def link_quality(self) -> tuple[int, int] | None:
        """(readings, damaged readings) over the last QUALITY_S seconds; None before there are any."""
        now = time.monotonic()
        q = self._quality
        q.append((now, self.packets_received, self._assembler.rejected))
        while len(q) > 2 and now - q[1][0] > self.QUALITY_S:
            q.popleft()
        good, bad = q[-1][1] - q[0][1], q[-1][2] - q[0][2]
        return (good, bad) if good + bad else None

    # ---- meter clock measurement (custom C-1.09+, CF-10) --------------------

    def supports_clock_report(self) -> bool:
        return self.key_resend_safe()       # the same versions: custom C-1.09 and later

    def measure_clock(self, on_result: Callable[[str], None]) -> None:
        """Ask the meter to measure its MSI clock against the 4 MHz crystal (key code 0x96, answer FB)."""
        if not self.is_connected:
            on_result("Not connected")
            return
        if not self.supports_clock_report():
            on_result("Clock measurement needs custom firmware C-1.09 or later")
            return
        if self.on_ld_page:
            on_result("Leave the page Ld first (the meter does not take key frames there)")
            return
        if self._clock_report is not None:
            return
        box = {"cb": on_result, "buf": bytearray()}
        box["timer"] = GLib.timeout_add(2500, self._clock_report_timeout, box)
        self._clock_report = box
        self.write_frame(CLOCK_REQUEST, None)

    def _match_clock_report(self, data: bytes) -> None:
        box = self._clock_report
        box["buf"] += data
        rep = parse_clock_report(bytes(box["buf"]))
        if rep is not None:
            GLib.source_remove(box["timer"])
            self._clock_report = None
            box["cb"](rep.text())
        elif len(box["buf"]) > 512:
            del box["buf"][:-40]

    def _clock_report_timeout(self, box: dict) -> bool:
        if self._clock_report is box:
            self._clock_report = None
            box["cb"]("The meter did not answer the clock measurement")
        return GLib.SOURCE_REMOVE

    # ---- key echo ------------------------------------------------------------

    KEY_ECHO_MS = 600       # the echo comes within about 100 ms (measured 2026-11-07: 60 of 60 within 0.4 s)

    def key_resend_safe(self) -> bool:
        """Custom C-1.09+ (CF-12) sends clean bytes on every page, so a missing echo means the key frame was lost and
        sending it again cannot press the key twice.  Older firmware damages about one echo in four (measured), so
        there a missing echo only gets a warning: a resend could toggle HOLD / REL back."""
        if getattr(self, "is_demo", False):
            return False
        return (custom_key(self.firmware_version) or (0, 0)) >= (1, 9)

    def confirm_key(self, frame: bytes, title: str, resend: Callable[[], None]) -> None:
        """Wait for the meter's echo of a key frame; without one, send it again once (key_resend_safe) or say that
        the meter did not confirm it.  Not on page Ld (key frames are not echoed there)."""
        if self.on_ld_page:
            return
        if self._key_wait is not None and self._key_wait.get("timer"):
            GLib.source_remove(self._key_wait["timer"])
        box = {"frame": bytes(frame), "title": title, "tail": bytearray(), "tries": 0, "resend": resend}
        box["timer"] = GLib.timeout_add(self.KEY_ECHO_MS, self._key_timed_out, box)
        self._key_wait = box

    def _match_key_echo(self, data: bytes) -> None:
        box = self._key_wait
        tail = box["tail"]
        tail += data
        if box["frame"] in tail:
            if box.get("timer"):
                GLib.source_remove(box["timer"])
            self._key_wait = None
            if box["tries"]:
                self.set_status(f"Sent {box['title']} (again: the first frame was lost)")
        elif len(tail) > 64:
            del tail[:-8]

    def _key_timed_out(self, box: dict) -> bool:
        box["timer"] = 0
        if self._key_wait is not box:
            return GLib.SOURCE_REMOVE
        if box["tries"] == 0 and self.key_resend_safe() and self.is_connected:
            box["tries"] = 1
            box["tail"].clear()
            box["resend"]()
            box["timer"] = GLib.timeout_add(self.KEY_ECHO_MS, self._key_timed_out, box)
        else:
            self._key_wait = None
            self.set_status(f"{box['title']}: the meter did not confirm the key")
        return GLib.SOURCE_REMOVE

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

    # ---- card files and settings (page Ld, open firmware v1.0.5+ / C-1.06+) ---

    @property
    def card_ready(self) -> bool:
        """The meter takes card and settings requests now (page Ld shown, no transfer running)."""
        return self.is_connected and self.on_ld_page and not self.uploading

    def card_request(self, rtype: int, payload: bytes = b"") -> tuple[int, int, bytes]:
        """Send one request and wait for its answer: (status, field, data). Called from a worker thread only;
        raises CardError (status None) when no answer came in time."""
        with self._card_lock:
            ev = threading.Event()
            box: dict = {"ev": ev, "result": None}

            def send() -> bool:
                if not self.is_connected:
                    ev.set()
                    return GLib.SOURCE_REMOVE
                self._card_seq = 0x4000 | ((self._card_seq + 1) & 0x1FFF)
                box["seq"] = self._card_seq
                self._card_parser.clear()
                self._card_raw.clear()
                self._card_wait = box
                self._write_pieces(request_packet(rtype, self._card_seq, payload), None)
                return GLib.SOURCE_REMOVE

            GLib.idle_add(send)
            ev.wait(TIMEOUT_MS.get(rtype, 8000) / 1000)
            if box.get("damaged"):
                raise CardError("the answer arrived damaged", raw=box.get("raw"))   # asked again, copies combined
            GLib.idle_add(lambda: (setattr(self, "_card_wait", None) if self._card_wait is box else None,
                                   GLib.SOURCE_REMOVE)[1])
            if box["result"] is None:
                raise CardError("no answer from the meter (is Ld-bt still shown on it?)")
            return box["result"]

    STREAM_IDLE_S = 0.6             # no byte for this long: the stream is over (pieces come every 10 ms)
    STREAM_FIRST_S = 3.0            # wait this long for the first byte of a stream

    def card_stream(self, first: int, count: int, size: int) -> tuple[int | None, int, dict[int, bytes]]:
        """STREAM pieces first .. first + count - 1 of the staged READ data: (status or None if no answer came, field,
        {index: data}) with every piece that arrived intact. Called from a worker thread only."""
        with self._card_lock:
            box: dict = {"parser": StreamParser(), "pieces": {}, "status": None, "field": count, "last": None}
            ready = threading.Event()

            def send() -> bool:
                if self.is_connected:
                    self._card_seq = 0x4000 | ((self._card_seq + 1) & 0x1FFF)
                    box["seq"] = self._card_seq
                    box["t0"] = time.monotonic()
                    self._card_stream = box
                    self._write_pieces(request_packet(fast_types(self.firmware_version)[1], self._card_seq, stream_payload(first, count, size)),
                                       None)
                ready.set()
                return GLib.SOURCE_REMOVE

            GLib.idle_add(send)
            ready.wait(5)
            try:
                while self._card_stream is box and self.is_connected:
                    now = time.monotonic()
                    want = box["field"] if box["status"] == 0 else count
                    if box["status"] not in (None, 0) or len(box["pieces"]) >= want:
                        break
                    last = box["last"]
                    if (last is None and now - box["t0"] > self.STREAM_FIRST_S) or \
                            (last is not None and now - last > self.STREAM_IDLE_S):
                        break
                    time.sleep(0.05)
            finally:
                GLib.idle_add(lambda: (setattr(self, "_card_stream", None) if self._card_stream is box else None,
                                       GLib.SOURCE_REMOVE)[1])
            return box["status"], box["field"], dict(box["pieces"])

    def _card_answers(self, data: bytes) -> None:
        box = self._card_wait
        raw = self._card_raw
        raw += data
        if len(raw) > 300:
            del raw[:-300]
        got = [a for a in self._card_parser.feed(data) if a[0] == box.get("seq")]
        if not got:
            fixed = repair_reply(bytes(raw), box.get("seq", -1))
            if fixed is None:
                # bytes came but no answer passes its CRC: the meter sends an answer in one go (indications 15 ms
                # apart), so a pause after them means it was damaged on the way; ask again instead of waiting for the
                # timeout (the meter's Bluetooth module flips bits, measured: 2 of 6 answers on 2026-11-05)
                if _is_meter_frame(data):                # readings and other frames don't hold the answer back
                    return
                if box.get("gap_timer"):
                    GLib.source_remove(box["gap_timer"])
                    box["gap_timer"] = 0
                start = raw.find(0xB5)
                if 0 <= start and len(raw) - start >= 8 and len(raw) - start >= 8 + raw[start + 5]:
                    self._card_gap(box)                 # all of it is here (by its length byte): damaged
                    return
                now = time.monotonic()
                last = box.get("last_piece")
                if last is not None:
                    box["spacing"] = max(box.get("spacing", 0.0), now - last)
                box["last_piece"] = now
                wait = max(CARD_GAP_MS, int(3000 * box.get("spacing", 0.0)))
                box["gap_timer"] = GLib.timeout_add(wait, self._card_gap, box)
                return
            self.answers_repaired += 1
            got = [fixed]
        if box.get("gap_timer"):
            GLib.source_remove(box.pop("gap_timer"))
        self._probe_answer_at = time.monotonic()
        box["result"] = got[0][1:]
        self._card_wait = None
        box["ev"].set()

    def _card_gap(self, box: dict) -> bool:
        if box.get("gap_timer"):
            GLib.source_remove(box["gap_timer"])
        box.pop("gap_timer", None)
        if self._card_wait is box and box["result"] is None and 0x82 in self._card_raw:
            self._card_wait = None
            box["damaged"] = True
            box["raw"] = bytes(self._card_raw)
            self.card_damaged += 1
            box["ev"].set()
        return GLib.SOURCE_REMOVE

    # ---- firmware upload (open firmware v1.0.0+, SETUP page Ld) ------------

    @property
    def uploading(self) -> bool:
        return self._upload is not None or self._boot_job is not None

    def upload_firmware(self, data: bytes, on_progress: Callable[[float, int, int, float], None],
                        on_done: Callable[[bool, str], None], kind: int | None = None) -> None:
        """Send a firmware file to the meter's SD card; on_progress(fraction, sent, total, bytes/s), on_done(ok, text)."""
        if not self.is_connected:
            on_done(False, "Not connected")
            return
        self.cancel_upload()
        self._upload = UploadSession(data, kind)
        self._upload_parser.clear()
        self._answer_raw.clear()
        self._upload_progress, self._upload_done = on_progress, on_done
        self._upload_t0 = time.monotonic()
        self._upload_send()

    def cancel_upload(self) -> None:
        if self._upload is not None:
            abort = self._upload.abort_packet()
            self._upload_finish(False, "Upload cancelled")
            self._pieces_gen += 1                   # stop a packet still being written in pieces
            # after 200 ms without a byte the meter drops a half-received packet; then ABORT: back to waiting (Ld-bt)
            GLib.timeout_add(300, lambda: (self._write_pieces(abort, None), GLib.SOURCE_REMOVE)[1])

    def write_piece_size(self) -> int:
        """The largest write the link carries in one go (0 = any size)."""
        return 0

    def _write_pieces(self, payload: bytes, done: Callable[[str | None], None] | None) -> None:
        """Write payload in pieces of write_piece_size(), one after the other; done(error) after the last.

        With the 121GW's ATT MTU of 23 a 240-byte upload packet is otherwise one BlueZ long write (prepare + execute,
        18 bytes per round trip): measured 1.55 s per packet against 1.15 s in 20-byte writes at a 45 ms connection
        interval, 0.63 s against 0.40 s at 15 ms (where the long writes also lost 3 of 25 packets). The meter puts
        the bytes together again (a packet ends after 200 ms without a byte, firmware ble/bft.s)."""
        n = self.write_piece_size()
        if not n or len(payload) <= n:
            self.write_frame(payload, done)
            return
        pieces = [payload[i:i + n] for i in range(0, len(payload), n)]
        gen = self._pieces_gen

        def after(i: int):
            def cb(err: str | None) -> None:
                if gen != self._pieces_gen:
                    return
                if err or i + 1 == len(pieces):
                    if done:
                        done(err)
                    return
                self.write_frame(pieces[i + 1], after(i + 1))
            return cb

        self.write_frame(pieces[0], after(0))

    def _upload_send(self) -> None:
        up = self._upload
        if up is None:
            return
        step = up.step()
        self._upload_stop_timer()
        self._upload_timer = GLib.timeout_add(step.timeout_ms, self._upload_timed_out)
        self._pieces_gen += 1
        self._write_pieces(step.packet, self._upload_written)

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

    # ---- direct firmware update through zombu4's bootloader ----------------

    def install_firmware(self, data: bytes, on_progress: Callable[[float, int, int, float], None],
                         on_done: Callable[[bool, str], None], on_state: Callable[[str], None] | None = None,
                         kind: int = 0, version: int | None = None) -> None:
        """Write a firmware image straight into the meter's flash: restart into the bootloader's update mode (0x91)
        unless it is there, then the bootloader's transfer. The meter restarts into the new firmware afterwards."""
        if not self.is_connected:
            on_done(False, "Not connected")
            return
        self.cancel_upload()
        if self._boot_job is not None:
            self._boot_finish(False, "Replaced by a new update")
        self._boot_job = {"data": bytes(data), "progress": on_progress, "done": on_done, "state": on_state,
                          "asked_at": time.monotonic(), "dropped": False,
                          "version": image_version(data) if kind == 0 else None, "kind": kind, "bl_version": version}
        if self._boot_prev_auto is None and self.settings is not None:
            self._boot_prev_auto = bool(getattr(self.settings, "auto_reconnect", True))
            self.set_auto_reconnect(True)           # the meter restarts: the link has to come back by itself
        self._boot_wait_timer = GLib.timeout_add_seconds(UPDATE_MODE_WAIT_S, self._boot_wait_timed_out)
        if self.update_mode:
            self._boot_begin()
            return
        self._boot_state("Restarting the meter into its update mode…")
        self.write_frame(UPDATE_MODE_REQUEST, None)

    def _boot_state(self, text: str) -> None:
        job = self._boot_job
        if job is not None and job["state"]:
            job["state"](text)

    def _boot_after_end(self) -> None:
        """The link dropped after the whole file was sent (the meter checks it and restarts, sometimes before its END
        answer gets out): wait for the meter to come back and judge by what it reports."""
        job = self._boot_job
        if self._boot_timer:
            GLib.source_remove(self._boot_timer)
            self._boot_timer = 0
        self._boot = None
        self._boot_tx.clear()
        self._boot_writing = False
        self._boot_parser.clear()
        job["after_end"] = job["dropped"] = True
        self._boot_state("The meter restarted before its answer came: waiting for it to come back…")
        if self._boot_wait_timer:
            GLib.source_remove(self._boot_wait_timer)
        self._boot_wait_timer = GLib.timeout_add_seconds(UPDATE_MODE_WAIT_S, self._boot_wait_timed_out)

    def _boot_came_back(self, version: str) -> None:
        job = self._boot_job
        want = job.get("version")
        if want is None or version == want:
            self._boot_finish(True, f"Installed: the meter runs {version}.")
        else:
            self._boot_finish(False, f"The meter runs {version}, not {want}: the update did not take. Send it again.")

    def _boot_wait_timed_out(self) -> bool:
        self._boot_wait_timer = 0
        if self._boot_job is not None and self._boot is None:
            if self._boot_job.get("after_end"):
                self._boot_finish(False, "The meter did not come back after the install. Look at it: with -IAP- on "
                                         "the display, connect again and send the file again.")
            else:
                self._boot_finish(False, "The meter did not come back in its update mode. If it shows -IAP-, connect "
                                         "again and send the file; otherwise use the SD card route.")
        return GLib.SOURCE_REMOVE

    def _boot_begin(self) -> None:
        job = self._boot_job
        if self._boot_wait_timer:
            GLib.source_remove(self._boot_wait_timer)
            self._boot_wait_timer = 0
        self._boot = BootSession(job["data"], job.get("kind", 0), job.get("bl_version"))
        self._boot_parser.clear()
        self._answer_raw.clear()
        self._boot_tx.clear()
        job["t0"] = time.monotonic()
        self._boot_state(f"Update mode ({self.update_mode}): erasing the firmware…")
        self._boot_restart_timer()
        self._boot_pump()

    def _boot_pump(self) -> None:
        s = self._boot
        if s is None:
            return
        for p in s.to_send():
            self._boot_tx.append((int.from_bytes(p[3:5], "little") if p[2] == T_DATA else -1, p))
        if self._boot_writing or not self._boot_tx:
            return
        _seq, p = self._boot_tx.popleft()
        self._boot_writing = True
        session = s

        def written(err: str | None) -> None:
            if self._boot is not session:
                return
            self._boot_writing = False
            if err:
                self._boot_finish(False, f"Bluetooth write failed: {err}. If the meter shows -IAP-, send the file "
                                         "again.")
                return
            self._boot_pump()

        self._write_pieces(p, written)

    def _boot_answer(self, seq: int, status: int, nxt: int) -> None:
        s = self._boot
        if s is None:
            return
        phase = s.phase
        if not s.answer(seq, status, nxt):
            return
        if s.done:
            self._boot_finish(s.ok, s.message)
            return
        if s.phase == "data":
            # packets queued but not yet written that the session will send again (or no longer needs)
            self._boot_tx = deque((n, p) for n, p in self._boot_tx if n < s.next_send and n >= s.acked)
        if phase == "start" and s.phase == "data":
            self._boot_state("Sending the firmware…")
        self._boot_restart_timer()
        job = self._boot_job
        if job and job["progress"]:
            elapsed = max(time.monotonic() - job["t0"], 1e-6)
            job["progress"](s.fraction, s.sent_bytes, len(s.data), s.sent_bytes / elapsed)
        if s.phase == "end" and phase == "data":
            self._boot_state("The bootloader checks the firmware…")
        self._boot_pump()

    def _boot_restart_timer(self) -> None:
        if self._boot_timer:
            GLib.source_remove(self._boot_timer)
        self._boot_timer = GLib.timeout_add(self._boot.timeout_ms, self._boot_timed_out)

    def _boot_timed_out(self) -> bool:
        self._boot_timer = 0
        s = self._boot
        if s is None:
            return GLib.SOURCE_REMOVE
        if s.timeout():
            self._boot_tx.clear()
            self._boot_restart_timer()
            self._boot_pump()
        else:
            self._boot_finish(False, s.message)
        return GLib.SOURCE_REMOVE

    def cancel_install(self) -> None:
        if self._boot_job is None:
            return
        sending = self._boot is not None
        self._boot_finish(False, "Update cancelled" + (": the firmware is erased, the meter stays in its update mode "
                                                       "(-IAP-) until a firmware is sent" if sending else ""))
        if sending:
            GLib.timeout_add(300, lambda: (self._write_pieces(BootSession(b"").abort_packet(), None),
                                           GLib.SOURCE_REMOVE)[1])

    def _boot_finish(self, ok: bool, text: str) -> None:
        for attr in ("_boot_timer", "_boot_wait_timer"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        job = self._boot_job
        self._boot_job = self._boot = None
        self._boot_tx.clear()
        self._boot_writing = False
        self._boot_parser.clear()
        if self._boot_prev_auto is not None:
            prev, self._boot_prev_auto = self._boot_prev_auto, None
            if not prev:                            # back after the meter's restart into the new firmware
                GLib.timeout_add_seconds(60, lambda: (self.set_auto_reconnect(False), GLib.SOURCE_REMOVE)[1])
        if job is not None and job["done"]:
            job["done"](ok, text)
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
