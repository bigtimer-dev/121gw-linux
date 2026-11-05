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
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .upload import crc16, packet

T_LIST, T_READ, T_CHUNK, T_DELETE, T_INFO, T_GET, T_PUT = 0x10, 0x11, 0x12, 0x13, 0x14, 0x16, 0x17
T_ANSWER = 0x82
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
              T_PUT: 8_000}
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._ -")


class CardError(RuntimeError):
    def __init__(self, text: str, status: int | None = None) -> None:
        super().__init__(text)
        self.status = status


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
    return str(v)


B20 = 0x020
SETTINGS: tuple[Setting, ...] = (
    Setting(0x01E, "3", "Buzzer", 0, 1, ("off", "on"), "Sound"),
    Setting(0x014, "10", "Continuity threshold", 0, 15, None, "Sound", help="Code 0..15 of the BEEP page"),
    Setting(0x021, "bH", "Continuity beep hold", 0, 3, ("100 ms", "50 ms", "25 ms", "off (15 ms)"), "Sound"),
    Setting(0x016, "2", "Auto power-off", 0, 1, ("on", "disabled"), "Power"),
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
    Setting(0x015, "bt", "Bluetooth readings", 0, 3, ("2 per second", "4 per second", "10 per second",
                                                        "20 per second"), "Logging and Bluetooth"),
    Setting(0x220, "tr", "Logging trigger", 0, 2, ("interval", "on change", "on limit verdict"),
            "Logging and Bluetooth", wire=B20, shift=1, mask=0x03),
    Setting(0x022, "Ch", "Logging change threshold", 1, 255, None, "Logging and Bluetooth"),
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
    return (old & ~(s.mask << s.shift) & 0x0F) | (value << s.shift)


# ---- the operations (worker thread) -----------------------------------------------------------------------------

Request = Callable[[int, bytes], "tuple[int, int, bytes]"]     # (type, payload) -> (status, field, data); raises


class CardClient:
    """Card and settings operations; `request` sends one request and returns its answer or raises CardError."""

    RETRIES = 4
    CHUNK_RETRIES = 8
    WINDOW_TRIES = 3

    def __init__(self, request: Request, cancelled: Callable[[], bool] = lambda: False) -> None:
        self._request = request
        self.cancelled = cancelled

    def _ask(self, rtype: int, payload: bytes = b"", retries: int | None = None) -> tuple[int, int, bytes]:
        last = None
        for _ in range(retries or self.RETRIES):
            if self.cancelled():
                raise CardError("cancelled")
            try:
                return self._request(rtype, payload)
            except CardError as e:
                if e.status is not None:
                    raise
                last = e
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
            for _ in range(self.CHUNK_RETRIES):
                try:
                    status, field, data = self._ask(T_CHUNK, chunk_payload(idx), retries=1)
                except CardError as e:
                    if e.status is not None or self.cancelled():
                        raise
                    continue
                if status:
                    raise CardError(f"chunk {idx}: {describe(status, field)}", status)
                if field == idx and len(data) == want:
                    buf += data
                    break
            else:
                raise CardError(f"chunk {idx} did not arrive")
        return bytes(buf)

    def read_window(self, name: str, offset: int, length: int) -> bytes:
        for _ in range(self.WINDOW_TRIES):
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
                    data = self.read_window(f.name, offset, min(WINDOW, f.size - offset))
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
        """The meter's raw setting bytes by wire id."""
        field, data = self._ok(T_GET)
        return parse_get(field, data)

    def put_setting(self, wire_id: int, value: int) -> int:
        field, data = self._ok(T_PUT, put_payload(wire_id, value))
        if field != wire_id or not data:
            raise CardError("the meter answered for another setting")
        return data[0]


_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def check_local_name(name: str) -> None:
    check_name(name)
    if name == MANIFEST or name.lower().endswith(".part") or name.split(".")[0].upper() in _DEVICES:
        raise CardError(f"{name!r} cannot be used as a local file name", ST_NAME)
