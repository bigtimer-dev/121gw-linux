"""Continuity / diode beeper with ON/OFF ohm hysteresis."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import time
import wave
from pathlib import Path

from .packet import PacketV2
from .settings import Settings, data_dir

MIN_BEEP_INTERVAL = 0.45


def _beep_file() -> Path:
    path = data_dir() / "beep.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rate, freq, dur = 44100, 2700.0, 0.4
    n = int(rate * dur)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / 200, (n - i) / 400)
        frames += struct.pack("<h", int(0.35 * 32767 * env * math.sin(2 * math.pi * freq * i / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class Beeper:
    def __init__(self) -> None:
        self._player = next((p for p in ("pw-play", "paplay", "aplay") if shutil.which(p)), None)
        self._proc: subprocess.Popen | None = None
        self._file: Path | None = None

    def beep(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return  # previous tone still sounding
        if self._player is None:
            print("\a", end="", flush=True)
            return
        if self._file is None:
            self._file = _beep_file()
        args = [self._player, str(self._file)]
        if self._player == "aplay":
            args.insert(1, "-q")
        try:
            self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._player = None


class ContinuityEngine:
    def __init__(self, settings: Settings, beeper: Beeper | None = None):
        self.settings = settings
        self.beeper = beeper or Beeper()
        self.is_closed = False
        self._last_beep = 0.0

    def evaluate(self, packet: PacketV2) -> None:
        if packet.is_continuity_mode or packet.is_resistance_mode:
            self._evaluate_ohms(packet.si_value)
        elif packet.is_diode_mode:
            self._evaluate_diode(packet)
        else:
            self.is_closed = False

    def _evaluate_ohms(self, ohms: float) -> None:
        if not math.isfinite(ohms) or ohms < 0:
            self.is_closed = False
            return
        on_t = self.settings.cont_on_threshold
        off_t = max(self.settings.cont_off_threshold, on_t + 1)
        if self.is_closed:
            if ohms > off_t:
                self.is_closed = False
            else:
                self._trigger(False)
        elif ohms < on_t:
            self.is_closed = True
            self._trigger(True)

    def _evaluate_diode(self, packet: PacketV2) -> None:
        if packet.is_main_overflow or not math.isfinite(packet.main_value):
            self.is_closed = False
            return
        if not self.is_closed:
            self.is_closed = True
            self._trigger(True)
        else:
            self._trigger(False)

    def reset(self) -> None:
        self.is_closed = False

    def _trigger(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_beep < MIN_BEEP_INTERVAL:
            return
        self._last_beep = now
        if self.settings.cont_sound:
            self.beeper.beep()
