"""Persistent app settings (~/.config/121gw/settings.json)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "121gw"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "121gw"


DEFAULTS = {
    "auto_reconnect": True,
    "last_device": None,          # BlueZ device address, e.g. "88:6B:0F:12:34:56"
    "show_all_devices": False,    # list every BLE device, not just 121GW
    "cont_sound": True,
    "cont_on_threshold": 25.0,
    "cont_off_threshold": 400.0,
    "notifications": True,        # desktop notifications for lost link / low battery
    "show_temperature": True,
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "settings.json"
        self._data = dict(DEFAULTS)
        try:
            self._data.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass
        self.normalize_thresholds()

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in DEFAULTS:
            raise AttributeError(name)
        return self._data.get(name, DEFAULTS[name])

    def __setattr__(self, name: str, value) -> None:
        if name in DEFAULTS:
            if self._data.get(name) != value:
                self._data[name] = value
                self.save()
        else:
            super().__setattr__(name, value)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)
        except OSError:
            pass

    def normalize_thresholds(self) -> None:
        on = self._data.get("cont_on_threshold", 25.0)
        off = self._data.get("cont_off_threshold", 400.0)
        if not isinstance(on, (int, float)) or not math.isfinite(on) or on < 0:
            on = 0.0
        if not isinstance(off, (int, float)) or not math.isfinite(off) or off < on + 1:
            off = on + 1
        self._data["cont_on_threshold"] = float(on)
        self._data["cont_off_threshold"] = float(off)


def parse_ohms(text: str) -> float | None:
    try:
        v = float(text.replace(",", ".").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None
