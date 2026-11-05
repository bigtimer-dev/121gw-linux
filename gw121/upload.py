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

T_START, T_DATA, T_END, T_ABORT, T_STATUS = 1, 2, 3, 4, 5
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


def repair_answer(buf: bytes, expected: list[int]) -> tuple[int, int, int] | None:
    """An answer whose first bytes the meter's Bluetooth module damaged, recovered from buf.

    On a real 121GW at a 7.5 ms connection interval about 1 in 6 answers arrives with one of its first bytes damaged
    (a one-bit slip on the module's serial input: 5B -> 2B or 56, 81 -> 60, the packet number 0x20 -> 0x10), while
    the meter had taken the packet. The rest (length, status, next, CRC-16) arrives intact. With the header and the
    awaited packet number put back, a matching CRC-16 proves the answer; nothing is guessed beyond that."""
    for i in range(len(buf) - 11, -1, -1):
        w = buf[i:i + 11]
        crc = int.from_bytes(w[9:11], "little")
        for seq in [int.from_bytes(w[3:5], "little"), *expected]:
            body = struct.pack("<BHB", 0x81, seq & 0xFFFF, 3) + w[6:9]
            if crc16(body) == crc:
                return seq, w[6], int.from_bytes(w[7:9], "little")
    return None


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

    def expected_seqs(self) -> list[int]:
        return [] if self.done else [self.step().seq]

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


PROBE_SEQ = 0x7E00                 # sequence number of the page-Ld probe (no transfer uses it)
BOOT_VERSION_INDEX = 0x80          # STATUS index only zombu4's bootloader answers (bootloader/src/bl_ble.s)


def probe_packet(n: int = 0) -> bytes:
    """STATUS request with the index 0x80: the firmware's page Ld refuses it (status 3; open firmware v1.0.0+, any state,
    nothing changes), the bootloader's update mode answers it with status 0 and its version; on every other page the
    meter's command reader drops it."""
    return packet(T_STATUS, PROBE_SEQ + (n & 0xFF), bytes([BOOT_VERSION_INDEX]))


def probe_result(status: int, nxt: int) -> str | None:
    """What a probe answer says: None for the page Ld, else the bootloader version in update mode ("B-1.11")."""
    if status != 0:
        return None
    return f"B-{(nxt >> 8) & 0xF}.{(nxt >> 4) & 0xF}{nxt & 0xF}"


# ---- direct update through zombu4's two-part bootloader (bl-v1.1.0+) ------------------------------------------------
#
# Firmware v1.0.4+ (and C-1.04+) understands two more key codes (firmware/src/ble/blcmd.s):
#   0x92  answered with F7 + "B-EEV" (EEVblog's bootloader) or "B-" + three digits (zombu4's) + XOR
#   0x91  restart into the bootloader's update mode; only with zombu4's bootloader (else nothing happens)
# In update mode ("-IAP-" on the display, no readings) the bootloader takes the image over Bluetooth with the same
# packets: START size(4) crc32(4) kind(1) (kind 0 firmware; the flash is erased before the answer), DATA with up to two
# packets in flight, END (the flash is checked; the meter restarts into the firmware half a second after the answer).

BOOTLOADER_INFO_REQUEST = bytes([0xF4, 0x39, 0x32, 0x39, 0x32])     # key code 0x92
UPDATE_MODE_REQUEST = bytes([0xF4, 0x39, 0x31, 0x39, 0x31])         # key code 0x91
KIND_FIRMWARE = 0
KIND_BOOTLOADER = 2                 # zombu4's bootloader image (blimage.bin) into the bootloader's spare slot
BOOT_WINDOW = 2
BOOT_TIMEOUT_START_MS = 30_000     # the bootloader erases the firmware before it answers START
BOOT_TIMEOUT_DATA_MS = 3_000
BOOT_TIMEOUT_END_MS = 30_000
BOOT_STATUS_TEXT = {
    1: "CRC wrong", 2: "not the expected packet", 3: "not allowed now", 4: "START refused (size or kind)",
    5: "more bytes than announced", 0x12: "length or CRC-32 of the written image wrong",
    0x13: "not a firmware image", 0x16: "flash write or check failed",
}


def parse_bootloader_answer(buf: bytes) -> str | None:
    """The bootloader from the first valid answer to 0x92 in buf: "B-EEV" (EEVblog's) or "B-1.11" (zombu4's)."""
    for i in range(len(buf) - 6):
        f = buf[i:i + 7]
        if f[0] != 0xF7 or f[1:3] != b"B-":
            continue
        x = 0
        for b in f:
            x ^= b
        if x:
            continue
        c = f[3:6]
        if c == b"EEV":
            return "B-EEV"
        if all(0x30 <= d <= 0x39 for d in c):
            return f"B-{chr(c[0])}.{chr(c[1])}{chr(c[2])}"
    return None


def has_direct_update(bootloader: str | None) -> bool:
    """True for zombu4's two-part bootloader (it takes firmware over Bluetooth in its update mode)."""
    return bool(bootloader) and bootloader != "B-EEV"


class BootSession:
    """START, the DATA packets with up to two in flight, END: the bootloader's update protocol, without I/O.

    to_send() gives the packets to write now (in order); answer() / timeout() apply what came back. The meter answers
    every packet in order and only takes the packet it expects, so a status 2 names where to go on from; a packet sent
    again is sent once per position (later status 2 answers for it come from packets already in flight)."""

    def __init__(self, data: bytes, kind: int = KIND_FIRMWARE, version: int | None = None) -> None:
        self.data = bytes(data)
        self.kind = kind
        self.version = version        # the bootloader's A << 16 | B << 8 | C, sent with a bootloader image
        self.chunks = [self.data[i:i + MAX_PAYLOAD] for i in range(0, len(self.data), MAX_PAYLOAD)]
        self.phase = "start"
        self.acked = 0                # DATA packets the meter has taken
        self.next_send = 0
        self._resent_at = -1          # position last gone back to
        self._sent_phase = False      # START / END written
        self.tries = 0
        self.resends = 0
        self.done = False
        self.ok = False
        self.message = ""

    @property
    def timeout_ms(self) -> int:
        return {"start": BOOT_TIMEOUT_START_MS, "data": BOOT_TIMEOUT_DATA_MS}.get(self.phase, BOOT_TIMEOUT_END_MS)

    @property
    def sent_bytes(self) -> int:
        if self.phase == "start":
            return 0
        if self.phase == "data":
            return min(len(self.data), self.acked * MAX_PAYLOAD)
        return len(self.data)

    @property
    def fraction(self) -> float:
        return self.sent_bytes / len(self.data) if self.data else 0.0

    def expected_seqs(self) -> list[int]:
        if self.done:
            return []
        if self.phase == "start":
            return [0]
        if self.phase == "end":
            return [len(self.chunks)]
        return list(range(self.acked, max(self.next_send, self.acked + 1)))

    def to_send(self) -> list[bytes]:
        if self.done:
            return []
        if self.phase == "start":
            if self._sent_phase:
                return []
            self._sent_phase = True
            crc = zlib.crc32(self.data) & 0xFFFFFFFF
            start = struct.pack("<IIB", len(self.data), crc, self.kind)
            if self.version is not None:
                start += struct.pack("<I", self.version)
            return [packet(T_START, 0, start)]
        if self.phase == "end":
            if self._sent_phase:
                return []
            self._sent_phase = True
            return [packet(T_END, len(self.chunks))]
        out = []
        while self.next_send < len(self.chunks) and self.next_send - self.acked < BOOT_WINDOW:
            out.append(packet(T_DATA, self.next_send, self.chunks[self.next_send]))
            self.next_send += 1
        return out

    def _fail(self, text: str) -> None:
        self.done, self.ok, self.message = True, False, text

    def _go_back(self, n: int) -> bool:
        """Send again from packet n (once per position); True when that changes what is sent next."""
        if n == self._resent_at:
            return False
        self._resent_at = n
        self.next_send = n
        self.resends += 1
        return True

    def answer(self, seq: int, status: int, nxt: int) -> bool:
        """Apply one answer; True when progress was made or something is to be sent (call to_send())."""
        if self.done:
            return False
        if self.phase == "start":
            if seq != 0:
                return False
            if status:
                self._fail(f"START: {BOOT_STATUS_TEXT.get(status, f'status 0x{status:02X}')}")
                return True
            self.phase, self._sent_phase, self.tries = "data", False, 0
            return True
        if self.phase == "end":
            if seq != len(self.chunks):
                return False
            if status:
                self._fail(f"END: {BOOT_STATUS_TEXT.get(status, f'status 0x{status:02X}')}. The firmware is erased: "
                           "the meter stays in its update mode (-IAP-); send the file again.")
                return True
            self.done, self.ok = True, True
            self.message = "Firmware written and checked by the bootloader. The meter restarts into it now."
            return True
        if status == 0:
            if seq < self.acked or seq >= self.next_send:
                return False                          # an earlier packet's answer
            # an earlier packet sent again is answered with status 0 and the packet the meter expects
            self.acked, self.tries = max(seq + 1, min(nxt, self.next_send)), 0
            if self.acked >= len(self.chunks):
                self.phase, self._sent_phase = "end", False
            return True
        if status == 2:
            if not 0 <= nxt <= len(self.chunks):
                self._fail(f"the meter expects packet {nxt}, which does not exist")
                return True
            if nxt >= len(self.chunks):
                self.acked = nxt
                self.phase, self._sent_phase = "end", False
                return True
            if self.acked < nxt < self.next_send:
                self.acked = nxt                      # the packets before nxt arrived (their answers were lost);
                return True                           # nxt itself is on its way
            if nxt >= self.next_send:
                self.acked = self.next_send = nxt
                return True
            self.acked = nxt                          # nxt was lost (or the meter went back): send from there once
            return self._go_back(nxt)
        if status == 1:
            return self._go_back(self.acked)
        self._fail(f"packet {seq}: {BOOT_STATUS_TEXT.get(status, f'status 0x{status:02X}')}")
        return True

    def timeout(self) -> bool:
        """No answer in time: True to go on (call to_send()), False when the session has failed."""
        self.tries += 1
        if self.tries >= (RETRIES_DATA if self.phase == "data" else RETRIES):
            self._fail("The meter stopped answering. If it shows -IAP-, it is still in its update mode: send the "
                       "file again." if self.phase != "start" else
                       "The meter did not answer in its update mode (-IAP-).")
            return False
        if self.phase == "data":
            self._resent_at = -1
            self._go_back(self.acked)
        else:
            self._sent_phase = False
        return True

    def abort_packet(self) -> bytes:
        return packet(T_ABORT, 0)
