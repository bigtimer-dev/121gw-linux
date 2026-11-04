"""Firmware upload over Bluetooth to the meter's SD card (zombu4's open firmware v1.0.0 and later, IMP-28).

On the meter's SETUP page `Ld` (shown `Ld-bt`, after the page `bt`) the meter takes a file over the Bluetooth link and
stores it on its SD card as EEVBlog.bin: first as BTLOAD.TMP, then, after the length, the CRC-32 and the image header
check out, copied to EEVBlog.bin and read back. The meter never writes its flash itself: the update is the usual key
sequence afterwards (meter off, hold MEM + HOLD while switching on, press SETUP).

Protocol (firmware/src/ble/bft.s, the same as zombu4's emulator/tools/btft.py; little-endian):
  packet  B5 5B type seq(2) len(1) payload(<= 232) crc16(2)   CRC-16/CCITT-FALSE over type .. payload
          type 1 START (size(4) crc32(4)), 2 DATA (seq = packet number), 3 END, 4 ABORT
  answer  B5 5B 81 seq(2) 03 status next(2) crc16(2)
Stop and wait: one packet, its answer, the next packet.

UploadSession is the protocol state machine without any I/O, so it can be tested against the emulated meter;
MeterLink.upload_firmware drives it over the real link.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

T_START, T_DATA, T_END, T_ABORT = 1, 2, 3, 4
MAX_PAYLOAD = 232
MIN_SIZE, MAX_SIZE = 0x124, 0x5A000
RETRIES = 8
RETRIES_DATA = 20                 # DATA without an answer: 20 x 3 s of silence (card writes take about a second)
TIMEOUT_START_MS = 5_000          # opens the temporary file on the card
# About 1.5 % of the DATA packets or their answers are lost on a real 121GW (MTU 23, 20-byte writes, measured
# 2026-11-04): with 20 s each those waits were 200 of the 476 s of an upload. The meter writes the card before it
# answers, so an answer comes within about a second; a packet sent again that had arrived gets "expected n+1".
TIMEOUT_DATA_MS = 3_000
TIMEOUT_END_MS = 120_000          # check, copy and read back of the whole file

STATUS_TEXT = {
    1: "CRC wrong", 2: "not the expected packet", 3: "not allowed now (is the meter on the SETUP page Ld?)",
    4: "file size refused", 5: "more bytes than announced",
    0x11: "the SD card could not be written", 0x12: "length or CRC-32 of the received file wrong",
    0x13: "not a firmware image", 0x14: "copy to EEVBlog.bin failed", 0x15: "EEVBlog.bin read back differs",
    0x16: "the received file could not be read back",
}
CARD_STEP = {1: "open", 2: "seek", 3: "write", 4: "short write (card full?)", 5: "close", 6: "read back"}


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def packet(ptype: int, seq: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload above {MAX_PAYLOAD} bytes")
    body = struct.pack("<BHB", ptype, seq & 0xFFFF, len(payload)) + bytes(payload)
    return b"\xB5\x5B" + body + struct.pack("<H", crc16(body))


def check_image(data: bytes) -> str | None:
    """The checks the meter makes at the end, made before sending: None, or why the file is refused."""
    if not MIN_SIZE <= len(data) <= MAX_SIZE:
        return f"size {len(data)} bytes is outside {MIN_SIZE} .. {MAX_SIZE}"
    sp, reset = struct.unpack_from("<II", data, 0)
    if not 0x20000001 <= sp <= 0x20014000:
        return f"initial stack pointer 0x{sp:08X} is not in the meter's RAM: not a 121GW firmware"
    if not (reset & 1) or not 0x08006000 <= reset < 0x08060000:
        return f"reset vector 0x{reset:08X} is outside the application area: not a 121GW firmware"
    return None


def image_version(data: bytes) -> str | None:
    """The version in the image ("C-1.02"): open firmware v1.0.1+ keeps its version answer (F6 + 5 characters + XOR,
    ble/commands.s) as a table in the image. None for older images and v2.05."""
    from .keys import parse_version_answer
    return parse_version_answer(data)


class AnswerParser:
    """Collects bytes from the meter and returns complete answers (seq, status, next); other bytes are skipped."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def clear(self) -> None:
        self.buf.clear()

    def feed(self, data: bytes) -> list[tuple[int, int, int]]:
        self.buf.extend(data)
        out = []
        while True:
            i = self.buf.find(b"\xB5\x5B\x81")
            if i < 0:
                del self.buf[: max(0, len(self.buf) - 2)]
                return out
            if len(self.buf) < i + 11:
                del self.buf[:i]
                return out
            a = bytes(self.buf[i:i + 11])
            if a[5] == 3 and crc16(a[2:9]) == struct.unpack_from("<H", a, 9)[0]:
                out.append((struct.unpack_from("<H", a, 3)[0], a[6], struct.unpack_from("<H", a, 7)[0]))
                del self.buf[: i + 11]
            else:
                del self.buf[: i + 1]


@dataclass
class Step:
    packet: bytes
    seq: int
    timeout_ms: int


class UploadSession:
    """START, every DATA packet, END; one packet in flight. done / ok / message tell the outcome."""

    def __init__(self, data: bytes) -> None:
        self.data = bytes(data)
        self.chunks = [self.data[i:i + MAX_PAYLOAD] for i in range(0, len(self.data), MAX_PAYLOAD)]
        self.phase = "start"
        self.n = 0                    # next DATA packet
        self.tries = 0
        self.done = False
        self.ok = False
        self.message = ""
        self.repeated_card_sessions = 0

    # ---- what to send ------------------------------------------------------

    def step(self) -> Step:
        if self.phase == "start":
            crc = zlib.crc32(self.data) & 0xFFFFFFFF
            return Step(packet(T_START, 0, struct.pack("<II", len(self.data), crc)), 0, TIMEOUT_START_MS)
        if self.phase == "data":
            return Step(packet(T_DATA, self.n, self.chunks[self.n]), self.n, TIMEOUT_DATA_MS)
        return Step(packet(T_END, len(self.chunks)), len(self.chunks), TIMEOUT_END_MS)

    @property
    def sent_bytes(self) -> int:
        if self.phase == "start":
            return 0
        if self.phase == "data":
            return min(len(self.data), self.n * MAX_PAYLOAD)
        return len(self.data)

    @property
    def fraction(self) -> float:
        return self.sent_bytes / len(self.data) if self.data else 0.0

    # ---- what came back ----------------------------------------------------

    def _fail(self, text: str) -> None:
        self.done, self.ok, self.message = True, False, text

    def answer(self, seq: int, status: int, nxt: int) -> bool:
        """Apply one answer; True when it advanced the session (send step() next), False when it was ignored."""
        if self.done:
            return False
        if status == 1:                                   # CRC wrong (its seq may be damaged too): send again
            return self._retry("the meter received a damaged packet")
        if seq != self.step().seq:
            return False                                  # a late answer to an earlier packet
        self.tries = 0
        where = {"start": "START", "data": f"packet {self.n}", "end": "END"}[self.phase]
        if self.phase == "data" and status == 2:
            if 0 <= nxt < len(self.chunks):
                self.n = nxt                              # the meter names the packet it expects
                return True
            self._fail(f"{where}: the meter expects packet {nxt}, which does not exist")
            return True
        if status == 0x11:
            step = CARD_STEP.get(nxt >> 8, str(nxt >> 8))
            self._fail(f"{where}: {STATUS_TEXT[status]} (step {step}, FatFs result {nxt & 0xFF})")
            return True
        if status in (0x14, 0x15):
            self._fail(f"{STATUS_TEXT[status]}. EEVBlog.bin on the card may be missing or incomplete: do NOT start "
                       "the update; send the file again or copy it with a computer.")
            return True
        if status:
            self._fail(f"{where}: {STATUS_TEXT.get(status, f'status 0x{status:02X}')}")
            return True
        if self.phase == "start":
            self.phase = "data"
        elif self.phase == "data":
            self.n += 1
            if self.n >= len(self.chunks):
                self.phase = "end"
        else:
            self.repeated_card_sessions = nxt
            self.done, self.ok = True, True
            self.message = ("EEVBlog.bin is on the meter's SD card and verified. To install it: switch the meter off, "
                            "hold MEM + HOLD while switching it on, then press SETUP.")
        return True

    def timeout(self) -> bool:
        """No answer in time: True to send step() again, False when the session has failed."""
        return self._retry("no answer")

    def _retry(self, why: str) -> bool:
        self.tries += 1
        if self.tries >= (RETRIES_DATA if self.phase == "data" else RETRIES):
            if self.phase == "start":
                self._fail("The meter did not answer. On the meter: Bluetooth on, SETUP page Ld (shows Ld-bt, after "
                           "the page bt), logging off. The page needs zombu4's open firmware v1.0.0 or later.")
            else:
                self._fail(f"{why} after {self.tries} attempts")
            return False
        return True

    def abort_packet(self) -> bytes:
        return packet(T_ABORT, 0)
