"""The meter's SD card and SETUP settings over Bluetooth (zombu4's open firmware v1.0.5+ and C-1.06+, page Ld-bt).

While the meter shows the SETUP page Ld (Bluetooth on, logging off) it takes requests with the packet framing of the
firmware upload (gw121/upload.py) and answers them with a type 0x82 answer (firmware ble/bftsd.s, IMP-36, and
ble/bftset.s, IMP-37):

  request  B5 5B type seq(2) len(1) payload crc16(2)
  answer   B5 5B 82 seq(2) len(1) status(1) field(2) data crc16(2)     len = 3 + len(data), data <= 232 bytes

  0x10 LIST    start(2)                     field = count | 0x8000 at the end of the directory;
                                            data = count x size(4) fdate(2) ftime(2) attr(1) namelen(1) name
  0x11 READ    namelen(1) name offset(4) len(2)   len 1..8192 staged in the meter; field = bytes read, data = CRC-32
  0x12 CHUNK   idx(2)                       field = idx, data = staged bytes idx*232 ..
  0x13 DELETE  namelen(1) name              root folder only; cal.bin, EEVBlog.bin and BTLOAD.TMP are protected
  0x14 INFO    -                            field = sectors per cluster, data = free clusters(4) total clusters(4)
  0x16 GET     -                            field = count, data = count x id(2) value(1)
  0x17 PUT     id(2) value(1)               field = id, data = the value in effect; the meter stores it as its own
                                            SETUP page does (the app never writes the EEPROM itself)

Everything here is free of I/O: CardClient runs the operations in a worker thread over a `request` callable that the
link provides (stop and wait, one request at a time). Every request is idempotent except DELETE; a DELETE whose answer
was lost and that then reports "not found" was done. Downloaded data is used only when its CRC-32 matches, and files
are written as <name>.part and renamed when complete; the sync never deletes or changes local files.
"""

from __future__ import annotations

import json
import os
import re
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .upload import crc16, packet

T_LIST, T_READ, T_CHUNK, T_DELETE, T_INFO, T_GET, T_PUT = 0x10, 0x11, 0x12, 0x13, 0x14, 0x16, 0x17
T_FORMAT, FORMAT_WORD = 0x15, b"FORMAT"     # zombu4's F-1.09+ / C-1.08+ with the two-part bootloader (IMP-39)
T_ANSWER = 0x82
T_LINK, T_STREAM, T_PIECE = 0x1D, 0x1E, 0x83     # fast reads (custom firmware CF-08) since C-1.10
MID_LINK, MID_STREAM = 0x1A, 0x1B                  # the same in C-1.08 / C-1.09; zombu4's F-1.32+ uses 0x1A (READZ), 0x1B (WHERE)
OLD_LINK, OLD_STREAM = 0x18, 0x19                  # the same in C-1.07; zombu4's F-1.12 uses 0x18 to restart the meter
T_SETPAGE, MID_SETPAGE = 0x1F, 0x1C                # CF-09 SETPAGE on Ld: C-1.10+, C-1.08 / C-1.09 (F-1.33: 0x1C = SHOW)


def custom_key(version: str | None) -> tuple[int, int] | None:
    """(major, minor) of a custom version "C-A.BC" (C-1.10 -> (1, 10)), None for any other firmware."""
    m = re.fullmatch(r"C-(\d+)\.(\d+)", version or "")
    return (int(m[1]), int(m[2])) if m else None


def fast_types(version: str | None) -> tuple[int, int]:
    """(LINK, STREAM) request types for the firmware `version`: C-1.07 has them at 0x18 / 0x19, C-1.08 / C-1.09 at
    0x1A / 0x1B; every other firmware gets 0x1D / 0x1E, which one without CF-08 refuses (status 3).  0x18 is never sent
    to it, because zombu4's F-1.12 and C-1.08 restart into the bootloader's update mode on 0x18, and 0x1A / 0x1B are
    zombu4's READZ / WHERE since F-1.32 / F-1.33."""
    k = custom_key(version)
    if k == (1, 7):
        return OLD_LINK, OLD_STREAM
    if k in ((1, 8), (1, 9)):
        return MID_LINK, MID_STREAM
    return T_LINK, T_STREAM


def setpage_type(version: str | None) -> int:
    """CF-09 SETPAGE request type on the page Ld: 0x1C in C-1.08 / C-1.09, 0x1F since C-1.10."""
    return MID_SETPAGE if custom_key(version) in ((1, 8), (1, 9)) else T_SETPAGE


CHUNK = 232
WINDOW = 8192
MAX_NAME = 63
ST_OK, ST_NOT_NOW, ST_NOT_FOUND, ST_NAME, ST_ARG, ST_PROTECTED, ST_CARD = 0, 3, 0x20, 0x21, 0x22, 0x23, 0x24
STATUS_TEXT = {
    ST_NOT_NOW: "not allowed now (a setting is being edited on the meter, or a firmware transfer runs)",
    ST_NOT_FOUND: "not found on the card", ST_NAME: "name refused", ST_ARG: "value or argument refused",
    ST_PROTECTED: "protected: the meter needs this file", ST_CARD: "card error",
}
CARD_STEP = {1: "open", 2: "seek", 3: "read", 4: "delete", 5: "free space"}
ATTR_DIR, ATTR_VOLUME, ATTR_CUT = 0x10, 0x08, 0x80
PROTECTED = ("cal.bin", "eevblog.bin", "btload.tmp")
SKIP_SYNC = ("btload.tmp",)
MANIFEST = ".121gw-sync.json"
TIMEOUT_MS = {T_LIST: 15_000, T_READ: 30_000, T_CHUNK: 4_000, T_DELETE: 20_000, T_INFO: 30_000, T_GET: 5_000,
              T_PUT: 8_000, T_FORMAT: 180_000}   # FORMAT: f_mkfs writes the file system; the answer comes when done
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._ -")


class CardError(RuntimeError):
    def __init__(self, text: str, status: int | None = None, raw: bytes | None = None) -> None:
        super().__init__(text)
        self.status = status
        self.raw = raw              # the bytes of an answer that arrived damaged (for combine_copies)


def describe(status: int, field: int = 0) -> str:
    if status == ST_CARD:
        return f"card error ({CARD_STEP.get(field >> 8, field >> 8)}, FatFs result {field & 0xFF})"
    return STATUS_TEXT.get(status, f"status 0x{status:02X}")


# ---- answers ----------------------------------------------------------------------------------------------------

class ReplyParser:
    """Type 0x82 answers out of the received byte stream (answers arrive in pieces of 20 bytes)."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def clear(self) -> None:
        self.buf.clear()

    def feed(self, data: bytes) -> list[tuple[int, int, int, bytes]]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(b"\xB5\x5B\x82")
            if i < 0:
                del self.buf[:-2]
                return out
            del self.buf[:i]
            if len(self.buf) < 6:
                return out
            n = self.buf[5]
            if n < 3:
                del self.buf[:1]
                continue
            if len(self.buf) < 8 + n:
                return out
            body = bytes(self.buf[2:6 + n])
            if crc16(body) == int.from_bytes(self.buf[6 + n:8 + n], "little"):
                seq = int.from_bytes(body[1:3], "little")
                out.append((seq, body[4], int.from_bytes(body[5:7], "little"), bytes(body[7:])))
                del self.buf[:8 + n]
            else:
                del self.buf[:1]


class StreamParser:
    """Answers and stream pieces out of the received bytes: ("answer", seq, status, field, data) for type 0x81 / 0x82
    (a 0x81 answer is how firmware without STREAM refuses it: status 3), ("piece", idx, data) for type 0x83."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple]:
        self.buf += data
        out: list[tuple] = []
        while True:
            i = self.buf.find(b"\xB5\x5B")
            if i < 0:
                del self.buf[:-1]
                return out
            del self.buf[:i]
            if len(self.buf) < 6:
                return out
            kind, n = self.buf[2], self.buf[5]
            if kind not in (0x81, T_ANSWER, T_PIECE) or (kind != T_PIECE and n < 3):
                del self.buf[:1]
                continue
            if len(self.buf) < 8 + n:
                return out
            body = bytes(self.buf[2:6 + n])
            if crc16(body) != int.from_bytes(self.buf[6 + n:8 + n], "little"):
                del self.buf[:1]
                continue
            del self.buf[:8 + n]
            num = int.from_bytes(body[1:3], "little")
            if kind == T_PIECE:
                out.append(("piece", num, body[4:]))
            else:
                out.append(("answer", num, body[4], int.from_bytes(body[5:7], "little"), body[7:]))


def repair_reply(buf: bytes, seq: int) -> tuple[int, int, int, bytes] | None:
    """An answer to request `seq` whose first bytes the meter's Bluetooth module damaged (as upload.repair_answer):
    accepted only when its CRC-16 matches with the header and the sequence number put back."""
    for i in range(len(buf) - 10, -1, -1):
        n = buf[i + 5]
        if n < 3 or i + 8 + n > len(buf):
            continue
        rest = buf[i + 6:i + 6 + n]
        for s in (int.from_bytes(buf[i + 3:i + 5], "little"), seq):
            body = struct.pack("<BHB", T_ANSWER, s & 0xFFFF, n) + rest
            if crc16(body) == int.from_bytes(buf[i + 6 + n:i + 8 + n], "little") and s == seq:
                return s, rest[0], int.from_bytes(rest[1:3], "little"), bytes(rest[3:])
    return None


def combine_copies(frames: list[bytes], max_tries: int = 16) -> tuple[int, int, int, bytes] | None:
    """One good answer out of several damaged copies of the same answer (asked again with new sequence numbers).

    The meter's Bluetooth module damages bytes of most longer answers, about one in 35, at random places (measured
    2026-11-05 on the real meter: every 111-byte CHUNK answer, 2..4 bytes in a 104-byte LIST answer). Copies of one
    length are voted byte by byte; where no value has a majority, the values seen there are tried (at most max_tries
    mixes). A result counts only when its CRC-16 matches together with one copy's own sequence number and CRC, so a
    wrong vote does not pass (and file data is checked against the meter's CRC-32 as well)."""
    by_len: dict[int, list[bytes]] = {}
    for f in frames:
        if len(f) >= 11:
            by_len.setdefault(len(f), []).append(f)
    for size, group in sorted(by_len.items(), key=lambda kv: -len(kv[1])):
        if len(group) < 2:
            continue
        n = size - 8
        bodies = [f[6:size - 2] for f in group]                       # status, field, data
        vote = bytearray(bodies[0])
        open_pos: list[tuple[int, list[int]]] = []
        for i in range(len(vote)):
            vals = [b[i] for b in bodies]
            if len(set(vals)) == 1:
                continue
            counts = {v: vals.count(v) for v in set(vals)}
            top = max(counts.values())
            best = sorted(v for v, c in counts.items() if c == top)
            vote[i] = best[0]
            if len(best) > 1:
                open_pos.append((i, best))
        total = 1
        for _, vals in open_pos:
            total *= len(vals)
        if total > max_tries:
            continue
        tails = {(f[3:5], f[size - 2:]) for f in group}
        for k in range(total):
            for i, vals in open_pos:
                vote[i] = vals[k % len(vals)]
                k //= len(vals)
            for seq_b, crc_b in tails:
                body = bytes([T_ANSWER]) + seq_b + bytes([n]) + bytes(vote)
                if crc16(body) == int.from_bytes(crc_b, "little"):
                    return (int.from_bytes(seq_b, "little"), vote[0], int.from_bytes(vote[1:3], "little"),
                            bytes(vote[3:]))
    return None


# ---- requests ---------------------------------------------------------------------------------------------------

def check_name(name: str) -> None:
    """The meter's own name rules (bftsd.s)."""
    if not name or len(name) > MAX_NAME or any(c not in _NAME_CHARS for c in name) or ".." in name \
            or name != name.strip(" ") or name.endswith("."):
        raise CardError(f"name refused by the meter's rules: {name!r}", ST_NAME)


def is_protected(name: str) -> bool:
    return name.lower() in PROTECTED


def _name(name: str) -> bytes:
    check_name(name)
    b = name.encode("ascii")
    return bytes([len(b)]) + b


def list_payload(start: int) -> bytes:
    return struct.pack("<H", start)


def read_payload(name: str, offset: int, length: int) -> bytes:
    return _name(name) + struct.pack("<IH", offset, length)


def chunk_payload(idx: int) -> bytes:
    return struct.pack("<H", idx)


def delete_payload(name: str) -> bytes:
    return _name(name)


def put_payload(wire_id: int, value: int) -> bytes:
    return struct.pack("<HB", wire_id, value)


def stream_payload(first: int, count: int, size: int = CHUNK) -> bytes:
    return struct.pack("<HHB", first, count, size)


def request_packet(rtype: int, seq: int, payload: bytes = b"") -> bytes:
    return packet(rtype, seq, payload)


# ---- decoding ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CardFile:
    name: str
    size: int
    fdate: int
    ftime: int
    attr: int

    @property
    def is_file(self) -> bool:
        """A file the meter can read: not a folder or volume label, name not cut, size below 4 GiB."""
        return not self.attr & (ATTR_DIR | ATTR_VOLUME | ATTR_CUT) and self.size != 0xFFFFFFFF

    @property
    def protected(self) -> bool:
        return is_protected(self.name)

    @property
    def when(self) -> datetime | None:
        d, t = self.fdate, self.ftime
        try:
            return datetime(1980 + (d >> 9), (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63, min(59, (t & 31) * 2))
        except ValueError:
            return None


def parse_list(field: int, data: bytes) -> tuple[list[CardFile], bool]:
    count, end = field & 0x7FFF, bool(field & 0x8000)
    out, i = [], 0
    for _ in range(count):
        if i + 10 > len(data):
            raise CardError("LIST answer shorter than its entries")
        size, fdate, ftime, attr, n = struct.unpack_from("<IHHBB", data, i)
        i += 10
        name = data[i:i + n].decode("latin-1")
        i += n
        out.append(CardFile(name, size, fdate, ftime, attr))
    return out, end


def parse_info(field: int, data: bytes) -> tuple[int, int]:
    """(free bytes, total bytes)."""
    if len(data) < 8:
        raise CardError("INFO answer too short")
    free, total = struct.unpack_from("<II", data)
    return free * field * 512, total * field * 512


def parse_get(field: int, data: bytes) -> dict[int, int]:
    return {struct.unpack_from("<H", data, i)[0]: data[i + 2] for i in range(0, min(len(data), field * 3), 3)}


# ---- settings ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    id: int                 # row id (the wire id, or 0x120 / 0x220 / 0x320 for the three fields of 0x020)
    page: str               # the meter's page
    title: str
    lo: int
    hi: int
    choices: tuple[str, ...] | None = None
    group: str = "General"
    wire: int = -1
    shift: int = 0
    mask: int = 0xFF
    help: str = ""
    wide: bool = False      # a two-byte setting (F-1.33 / C-1.10: GET flags 1, PUT id(2) value(2))

    @property
    def wire_id(self) -> int:
        return self.id if self.wire < 0 else self.wire

    def label(self, v: int) -> str:
        if self.choices and self.lo <= v <= self.hi:
            return self.choices[v - self.lo]
        return value_text(self, v)


def value_text(s: Setting, v: int) -> str:
    if s.id == 0x017:
        return "off" if v == 0 else "30 s" if v == 1 else f"{v - 1} min"
    if s.id == 0x02A:
        return f"{(v - 100) * 0.2:+.1f} %"
    if s.id == 0x022:
        return f"{v} counts"
    if s.id == 0x02D:
        return f"{v} Ω"
    if s.id == 0x02F:
        return f"{v / 10:.1f} Ω"
    if s.id == 0x018:
        return f"{v} s"
    return str(v)


B20 = 0x020
SETTINGS: tuple[Setting, ...] = (
    Setting(0x01E, "3", "Buzzer", 0, 2, ("off", "on", "quiet (keys silent, measurement beeps on)"), "Sound",
            help="quiet: F-1.24 / C-1.10 or later"),
    Setting(0x014, "10", "Continuity threshold", 0, 17, None, "Sound",
            help="Code 0..15 of the BEEP page; 16 DNUSr / 17 UPUSr use th and HY (F-1.33 / C-1.10)"),
    Setting(0x02D, "th", "Continuity user threshold", 1, 500, None, "Sound", help="ohm, for DNUSr / UPUSr", wide=True),
    Setting(0x02F, "HY", "Continuity user hysteresis", 1, 250, None, "Sound", help="tenths of an ohm (50 = 5.0 Ω)"),
    Setting(0x02C, "CHIrP", "DIODE contact chirp", 0, 1, ("off (beep on every jump)", "on (one beep per contact)"),
            "Sound"),
    Setting(0x021, "bH", "Continuity beep hold", 0, 3, ("100 ms", "50 ms", "25 ms", "off (15 ms)"), "Sound"),
    Setting(0x016, "2", "Auto power-off", 0, 1, ("on", "disabled"), "Power"),
    Setting(0x02B, "2", "Auto power-off time", 0, 3, ("30 min", "1 min", "15 min", "60 min"), "Power",
            help="doubled while MIN/MAX is on"),
    Setting(0x017, "bL", "Backlight auto-off", 0, 100, None, "Power", help="0 = never, 1 = 30 s, n = n − 1 minutes"),
    Setting(0x02A, "BAT", "Battery voltage trim", 50, 150, None, "Power", help="Steps of 0.2 %"),
    Setting(0x01A, "4", "LCD contrast", 10, 17, None, "Display"),
    Setting(0x013, "dSP", "Display level", 0, 1, ("1 (steadiest)", "2 (quicker)"), "Display"),
    Setting(0x120, "rEL", "REL display", 0, 1, ("difference", "percent"), "Display", wire=B20, shift=0, mask=0x01),
    Setting(0x320, "CL", "Clock", 0, 1, ("24 hour", "12 hour"), "Display", wire=B20, shift=3, mask=0x01),
    Setting(0x010, "0", "Temperature unit", 0, 1, ("°F", "°C"), "Measuring"),
    Setting(0x00F, "tc", "Thermocouple type", 0, 5, ("K", "J", "T", "N", "R", "S"), "Measuring"),
    Setting(0x011, "r", "dBm reference (ACV)", 0, 3, ("600 Ω", "50 Ω", "75 Ω", "dBV"), "Measuring"),
    Setting(0x028, "LP", "Current loop view (DC mA)", 0, 2, ("off", "4–20 mA", "0–20 mA"), "Measuring"),
    Setting(0x029, "Pt", "RTD view (Ω)", 0, 2, ("off", "Pt100", "Pt1000"), "Measuring"),
    Setting(0x01B, "11", "VA burden “bd”", 0, 1, ("off", "on"), "Measuring"),
    Setting(0x420, "HUnt", "Capacitance anti-hunting", 0, 1, ("off", "on"), "Measuring", wire=B20, shift=4, mask=0x01,
            help="CAP auto range stays on 1 µF for a 10..100 nF capacitor that range 1 cannot time"),
    Setting(0x015, "bt", "Bluetooth readings", 0, 3, ("2 per second", "4 per second", "10 per second",
                                                        "20 per second"), "Logging and Bluetooth"),
    Setting(0x220, "tr", "Logging trigger", 0, 2, ("interval", "on change", "on limit verdict"),
            "Logging and Bluetooth", wire=B20, shift=1, mask=0x03),
    Setting(0x022, "Ch", "Logging change threshold", 1, 255, None, "Logging and Bluetooth"),
    Setting(0x018, "In", "Logging interval", 0, 999, None, "Logging and Bluetooth", help="seconds", wide=True),
)
SETTINGS_BY_ID = {s.id: s for s in SETTINGS}


def settings_values(raw: dict[int, int]) -> dict[int, int]:
    """Row values from the meter's GET answer (the three fields of 0x020 taken apart)."""
    out = {}
    for s in SETTINGS:
        if s.wire_id in raw:
            out[s.id] = (raw[s.wire_id] >> s.shift) & s.mask
    return out


def wire_value(s: Setting, value: int, raw: dict[int, int]) -> int:
    """The byte to PUT for row s set to value (read-modify-write of the shared 0x020)."""
    if not s.lo <= value <= s.hi:
        raise CardError(f"{s.title}: {value} is outside {s.lo}..{s.hi}", ST_ARG)
    if s.wire < 0:
        return value
    old = raw.get(s.wire, 0)
    return (old & ~(s.mask << s.shift) & 0x1F) | (value << s.shift)     # bits 4..0 (bit 4 HUnt, IMP-45)


# ---- the operations (worker thread) -----------------------------------------------------------------------------

Request = Callable[[int, bytes], "tuple[int, int, bytes]"]     # (type, payload) -> (status, field, data); raises
# (first, count, size) -> (status or None when no answer came, field, {piece index: data})
Stream = Callable[[int, int, int], "tuple[int | None, int, dict[int, bytes]]"]


class CardClient:
    """Card and settings operations; `request` sends one request and returns its answer or raises CardError."""

    RETRIES = 10
    # bytes per READ: the meter's Bluetooth module loses bytes of answers above about 130 bytes (its buffer overflows:
    # the meter sends at 19200 baud, faster than the module forwards), measured 2026-11-05: 111-byte answers all
    # complete, 151 bytes and more none; so one CHUNK answer of at most 100 data bytes per READ
    window = 100
    CHUNK_RETRIES = 12
    WINDOW_TRIES = 3
    WINDOW_ROUNDS = 5                # fresh rounds for one piece after a streak of damaged answers

    FAST_WINDOW = 8192               # bytes per READ with STREAM (the meter's staging buffer)
    # The meter's Bluetooth module sends one 20-byte indication per confirmation, i.e. per two connection events:
    # 1330 bytes/s at 7.5 ms, 660 at 15 ms (the adapter picks 15 ms while something scans for devices, measured
    # 2026-11-05). Pace 13 (the firmware's default, with an idle frame every 2 bytes) needs the 7.5 ms interval;
    # when a stream loses pieces the pace is lowered (LINK) so the module is not overrun.
    PACES = (13, 6, 3)
    STREAM_ROUNDS = 4                # STREAM again for pieces that did not arrive, then CHUNK for the rest

    def __init__(self, request: Request, cancelled: Callable[[], bool] = lambda: False,
                 stream: Stream | None = None, version: str | None = None) -> None:
        self._request = request
        self._t_link = fast_types(version)[0]
        self.cancelled = cancelled
        self._stream = stream
        # STREAM support: None not known yet, True the firmware streams (C-1.07 on: paced, idle gaps, measured
        # 1192 bytes/s without a damaged byte), False it does not (C-1.06, zombu4's firmware: 100-byte CHUNKs)
        self.fast: bool | None = None if stream else False
        self.combined = 0           # answers put together from damaged copies
        self.streamed = 0           # pieces that arrived by STREAM
        self.pace = self.PACES[0]   # bytes per 10 ms the meter sends (LINK); lowered when pieces get lost

    def _ask(self, rtype: int, payload: bytes = b"", retries: int | None = None) -> tuple[int, int, bytes]:
        """Ask until an answer passes its CRC-16; damaged copies of the answer are combined (combine_copies)."""
        last, copies = None, []
        for _ in range(retries or self.RETRIES):
            if self.cancelled():
                raise CardError("cancelled")
            try:
                return self._request(rtype, payload)
            except CardError as e:
                if e.status is not None:
                    raise
                last = e
                if e.raw:
                    copies.append(e.raw)
                    if len(copies) >= 2:
                        got = combine_copies(copies)
                        if got is not None:
                            self.combined += 1
                            return got[1:]
        raise last or CardError("no answer")

    def _ok(self, rtype: int, payload: bytes = b"") -> tuple[int, bytes]:
        status, field, data = self._ask(rtype, payload)
        if status:
            raise CardError(describe(status, field), status)
        return field, data

    # card
    def list_all(self) -> list[CardFile]:
        out, start = [], 0
        while True:
            entries, end = parse_list(*self._ok(T_LIST, list_payload(start)))
            out += entries
            if end:
                return out
            if not entries:
                raise CardError("the card list did not advance")
            start += len(entries)

    def files(self) -> list[CardFile]:
        return [f for f in self.list_all() if f.is_file]

    def info(self) -> tuple[int, int]:
        return parse_info(*self._ok(T_INFO))

    def format(self) -> None:
        """Format the card as exFAT, as the meter's page Fr does: every file on it is gone (the calibration in the
        meter's EEPROM is not touched). Sent once: the meter answers only when it has finished."""
        try:
            status, field, _ = self._request(T_FORMAT, FORMAT_WORD)
        except CardError as e:
            if e.raw:                       # an answer came, damaged: the meter has finished; the listing shows it
                return
            raise CardError("the meter did not answer the format in 3 minutes: look at the meter, then Refresh")
        if status == ST_NOT_NOW:
            raise CardError("format refused: the meter needs firmware F-1.09 / C-1.08 or later with zombu4's "
                            "two-part bootloader, and no firmware transfer may be running", status)
        if status:
            raise CardError(f"format failed: {describe(status, field)}", status)

    def delete(self, name: str) -> None:
        if is_protected(name):
            raise CardError(f"{name} is protected: the meter needs it", ST_PROTECTED)
        payload = delete_payload(name)
        for attempt in range(self.RETRIES):
            try:
                status, field, _ = self._request(T_DELETE, payload)
            except CardError as e:
                if e.status is not None or attempt == self.RETRIES - 1 or self.cancelled():
                    raise
                continue
            if status == ST_NOT_FOUND and attempt:
                return                      # an earlier send did it; only its answer was lost
            if status:
                raise CardError(describe(status, field), status)
            return

    def _chunks(self, n: int) -> bytes:
        buf = bytearray()
        for idx in range((n + CHUNK - 1) // CHUNK):
            want = min(CHUNK, n - idx * CHUNK)
            for _ in range(3):
                status, field, data = self._ask(T_CHUNK, chunk_payload(idx), retries=self.CHUNK_RETRIES)
                if status in (ST_NOT_NOW, ST_ARG):
                    raise CardError(f"chunk {idx}: {describe(status, field)}", status)
                if status:
                    continue                                 # a status CHUNK never has: a bad mix, ask again
                if field == idx and len(data) == want:
                    buf += data
                    break
            else:
                raise CardError(f"chunk {idx} did not arrive")
        return bytes(buf)

    def _slower(self) -> None:
        """Pieces got lost: the module forwards less than the meter sends; ask the meter for the next lower pace."""
        lower = [p for p in self.PACES if p < self.pace]
        if not lower:
            return
        try:
            status, _, _ = self._ask(self._t_link, struct.pack("<BBbB", lower[0], 1, 0, 2))
        except CardError:
            return
        if status == ST_OK:
            self.pace = lower[0]

    def _stream_window(self, field: int) -> bytes | None:
        """The `field` staged bytes by STREAM (pieces of 232 bytes = CHUNK indexes; what does not arrive after a few
        rounds is asked by CHUNK); None when the firmware has no STREAM (self.fast becomes False)."""
        n = (field + CHUNK - 1) // CHUNK
        got: dict[int, bytes] = {}
        for _ in range(self.STREAM_ROUNDS):
            missing = [i for i in range(n) if i not in got]
            if not missing:
                break
            runs, start = [], missing[0]
            for a, b in zip(missing, missing[1:] + [None]):
                if b != a + 1:
                    runs.append((start, a - start + 1))
                    start = b
            for first, count in runs:
                if self.cancelled():
                    raise CardError("cancelled")
                status, _, pieces = self._stream(first, count, CHUNK)
                if status == ST_NOT_NOW and self.fast is None and not pieces:
                    self.fast = False                       # no STREAM in this firmware
                    return None
                if status not in (None, ST_OK) and not pieces:
                    raise CardError(f"stream: {describe(status, 0)}", status)
                fresh = 0
                for i, d in pieces.items():
                    want = min(CHUNK, field - i * CHUNK)
                    if first <= i < first + count and len(d) == want:
                        got[i] = d
                        fresh += 1
                if pieces:
                    self.fast = True
                if self.fast and count >= 4 and fresh < count * 0.8:
                    self._slower()
        self.streamed += len(got)
        for i in range(n):
            if i not in got:
                status, fld, data = self._ask(T_CHUNK, chunk_payload(i), retries=self.CHUNK_RETRIES)
                if status or fld != i:
                    raise CardError(f"chunk {i}: {describe(status, fld)}", status or None)
                got[i] = data
        return b"".join(got[i] for i in range(n))

    def read_window(self, name: str, offset: int, length: int) -> bytes:
        for _ in range(self.WINDOW_TRIES):
            if self.fast is not False:
                field, data = self._ok(T_READ, read_payload(name, offset, length))
                if field == 0:
                    return b""
                if field > length or len(data) < 4:
                    raise CardError("READ answer out of range")
                got = self._stream_window(field)
                if got is None:
                    return self.read_window(name, offset, min(length, self.window))   # older firmware
                if zlib.crc32(got) & 0xFFFFFFFF == int.from_bytes(data[:4], "little"):
                    return got
                continue
            field, data = self._ok(T_READ, read_payload(name, offset, length))
            if field == 0:
                return b""
            if field > length or len(data) < 4:
                raise CardError("READ answer out of range")
            crc = int.from_bytes(data[:4], "little")
            for _ in range(2):
                got = self._chunks(field)
                if zlib.crc32(got) & 0xFFFFFFFF == crc:
                    return got
        raise CardError(f"{name}: the data at {offset} did not match its CRC-32")

    def download(self, f: CardFile, folder: Path, progress: Callable[[int], None] | None = None) -> Path:
        """Copy one card file into folder (written as .part, renamed when complete); progress(bytes done)."""
        check_local_name(f.name)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f.name
        part = folder / (f.name + ".part")
        try:
            with open(part, "wb") as out:
                offset = 0
                while offset < f.size:
                    for attempt in range(self.WINDOW_ROUNDS):
                        try:
                            size = self.FAST_WINDOW if self.fast is not False else self.window
                            data = self.read_window(f.name, offset, min(size, f.size - offset))
                            break
                        except CardError as e:
                            # a streak of damaged answers (no status from the meter): ask for this piece again
                            if e.status is not None or self.cancelled() or attempt == self.WINDOW_ROUNDS - 1:
                                raise
                    if not data:
                        raise CardError(f"{f.name} is shorter than listed ({offset} of {f.size} bytes)")
                    out.write(data)
                    offset += len(data)
                    if progress:
                        progress(offset)
            os.replace(part, path)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if f.when:
            ts = f.when.timestamp()
            os.utime(path, (ts, ts))
        return path

    def sync(self, folder: Path, progress: Callable[[str, int, int], None] | None = None) -> dict:
        """Copy new or changed card files into folder; never deletes or changes other local files.
        progress(name, bytes done in total, bytes to copy in total)."""
        folder.mkdir(parents=True, exist_ok=True)
        mpath = folder / MANIFEST
        try:
            done = json.loads(mpath.read_text("utf-8")).get("files", {})
            if not isinstance(done, dict):
                done = {}
        except (OSError, ValueError, AttributeError):
            done = {}
        todo, skipped, unchanged = [], [], 0
        for f in self.list_all():
            try:
                if not f.is_file or f.name.lower() in SKIP_SYNC:
                    raise CardError("not a file to copy")
                check_local_name(f.name)
            except CardError:
                skipped.append(f.name)
                continue
            rec, local = done.get(f.name), folder / f.name
            if isinstance(rec, dict) and (rec.get("size"), rec.get("fdate"), rec.get("ftime")) == \
                    (f.size, f.fdate, f.ftime) and local.is_file() and local.stat().st_size == f.size:
                unchanged += 1
            else:
                todo.append(f)
        total, base = sum(f.size for f in todo), 0
        for f in todo:
            self.download(f, folder, (lambda n, b=base, name=f.name: progress(name, b + n, total))
                          if progress else None)
            base += f.size
            done[f.name] = {"size": f.size, "fdate": f.fdate, "ftime": f.ftime}
            tmp = folder / (MANIFEST + ".part")
            tmp.write_text(json.dumps({"files": done}, indent=1, sort_keys=True), "utf-8")
            os.replace(tmp, mpath)
        return {"copied": [f.name for f in todo], "unchanged": unchanged, "skipped": skipped, "bytes": total}

    # settings
    def get_settings(self) -> dict[int, int]:
        """The meter's raw setting values by wire id: with two-byte values (GET start max flags 1, F-1.33 / C-1.10),
        else the one-byte GET of older firmware."""
        try:
            out, start = {}, 0
            for _ in range(16):
                field, data = self._ok(T_GET, bytes([start, 255, 1]))
                n = field & 0x7FFF
                for i in range(min(n, len(data) // 4)):
                    sid, v = struct.unpack_from("<HH", data, 4 * i)
                    out[sid] = v
                if field & 0x8000 or n == 0:
                    return out
                start += n
            return out
        except CardError as e:
            if e.status != ST_ARG:                   # older firmware: GET takes no payload (or start max only)
                raise
        field, data = self._ok(T_GET)
        return parse_get(field, data)

    def put_setting(self, wire_id: int, value: int, wide: bool = False) -> int:
        payload = struct.pack("<HH", wire_id, value) if wide else put_payload(wire_id, value)
        field, data = self._ok(T_PUT, payload)
        if field != wire_id or not data:
            raise CardError("the meter answered for another setting")
        return struct.unpack_from("<H", data)[0] if wide and len(data) >= 2 else data[0]


_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def check_local_name(name: str) -> None:
    check_name(name)
    if name == MANIFEST or name.lower().endswith(".part") or name.split(".")[0].upper() in _DEVICES:
        raise CardError(f"{name!r} cannot be used as a local file name", ST_NAME)
