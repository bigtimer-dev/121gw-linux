"""Measurement logging, session persistence and CSV export."""

from __future__ import annotations

import io
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .packet import PacketV2
from .settings import data_dir

LIVE_WINDOW = 25_000


@dataclass
class DataPoint:
    timestamp: float                 # unix seconds
    value: float
    unit: str
    display: str
    plot_value: float
    plot_unit: str
    plot_full_scale: float
    plot_lower_bound: float
    mode_raw: int = -1
    range_index: int = -1
    temperature_value: float | None = None
    temperature_unit: str | None = None

    @classmethod
    def from_packet(cls, p: PacketV2, ts: float | None = None) -> DataPoint:
        sub_temp = p.sub_is_temperature and math.isfinite(p.sub_value)
        return cls(
            timestamp=ts if ts is not None else datetime.now().timestamp(),
            value=p.main_value,
            unit=p.main_unit,
            display=p.display_string,
            plot_value=p.graph_value,
            plot_unit=p.graph_unit,
            plot_full_scale=p.graph_full_scale,
            plot_lower_bound=p.graph_lower_bound,
            mode_raw=p.mode_raw,
            range_index=p.main_range_index,
            temperature_value=p.sub_value if sub_temp else None,
            temperature_unit=p.sub_unit if p.sub_is_temperature else None,
        )


def _finite(points: list[DataPoint]) -> list[float]:
    return [p.plot_value for p in points if math.isfinite(p.plot_value)]


@dataclass
class Session:
    name: str
    points: list[DataPoint] = field(default_factory=list)
    notes: str = ""
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def duration(self) -> float:
        return 0.0 if len(self.points) < 2 else self.points[-1].timestamp - self.points[0].timestamp

    @property
    def graph_unit(self) -> str:
        for p in reversed(self.points):
            if p.plot_unit:
                return p.plot_unit
        return self.points[-1].unit if self.points else ""

    @property
    def temperature_unit(self) -> str | None:
        for p in self.points:
            if p.temperature_unit:
                return p.temperature_unit
        return None

    @property
    def has_temperature(self) -> bool:
        return any(p.temperature_value is not None and math.isfinite(p.temperature_value) for p in self.points)

    def stats(self) -> dict[str, float] | None:
        v = _finite(self.points)
        if not v:
            return None
        lo, hi = min(v), max(v)
        mean = sum(v) / len(v)
        var = sum((x - mean) ** 2 for x in v) / len(v)
        return {"min": lo, "max": hi, "avg": mean, "pp": hi - lo, "std": math.sqrt(var)}

    def csv_string(self) -> str:
        out = io.StringIO()
        out.write("timestamp,value,unit,display,plot_value,plot_unit,plot_full_scale,plot_lower_bound,"
                  "mode,range,temperature,temperature_unit\n")
        for p in self.points:
            t = datetime.fromtimestamp(p.timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            display = p.display.replace('"', '""')
            temp = "" if p.temperature_value is None else repr(p.temperature_value)
            out.write(f"{t},{_num(p.value)},{p.unit},\"{display}\",{_num(p.plot_value)},{p.plot_unit},"
                      f"{_num(p.plot_full_scale)},{_num(p.plot_lower_bound)},{p.mode_raw},{p.range_index},"
                      f"{temp},{p.temperature_unit or ''}\n")
        return out.getvalue()

    def to_json(self) -> dict:
        d = asdict(self)
        d["points"] = [asdict(p) for p in self.points]
        return d

    @classmethod
    def from_json(cls, d: dict) -> Session:
        pts = [DataPoint(**p) for p in d.get("points", [])]
        return cls(name=d.get("name", "Session"), points=pts, notes=d.get("notes", ""),
                   created_at=d.get("created_at", 0.0), id=d.get("id") or str(uuid.uuid4()))


def _num(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "NaN"
    return repr(float(v))


def default_session_name() -> str:
    return "Session " + datetime.now().strftime("%Y-%m-%d %H:%M")


class SessionStore:
    """Holds the in-progress log and the saved session list."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "sessions.json"
        self.sessions: list[Session] = []
        self.current: list[DataPoint] = []
        self.is_logging = False
        self.live_min: float | None = None
        self.live_max: float | None = None
        self.fingerprint: str | None = None
        self.load()

    # ---- live logging ------------------------------------------------------

    def _reset_current(self) -> None:
        self.current = []
        self.live_min = self.live_max = None
        self.fingerprint = None

    def start_logging(self) -> None:
        self._reset_current()
        self.is_logging = True

    def stop_logging(self) -> None:
        self.is_logging = False

    def discard_current(self) -> None:
        self._reset_current()
        self.is_logging = False

    def append(self, packet: PacketV2) -> bool:
        """Append a packet. Returns False if logging stopped because the function changed."""
        if not self.is_logging or not math.isfinite(packet.main_value):
            return True
        fp = packet.measure_fingerprint
        if self.fingerprint is not None and self.fingerprint != fp and self.current:
            self.is_logging = False
            return False
        if self.fingerprint is None:
            self.fingerprint = fp
        pt = DataPoint.from_packet(packet)
        self.current.append(pt)
        if math.isfinite(pt.plot_value):
            if self.live_min is None or pt.plot_value < self.live_min:
                self.live_min = pt.plot_value
            if self.live_max is None or pt.plot_value > self.live_max:
                self.live_max = pt.plot_value
        return True

    @property
    def live_window(self) -> list[DataPoint]:
        return self.current[-LIVE_WINDOW:]

    def save_current(self, name: str, notes: str = "") -> Session | None:
        if not self.current:
            return None
        s = Session(name=name or default_session_name(), points=list(self.current), notes=notes)
        self.sessions.insert(0, s)
        self.persist()
        self.discard_current()
        return s

    # ---- saved sessions ----------------------------------------------------

    def delete(self, session: Session) -> None:
        self.sessions = [s for s in self.sessions if s.id != session.id]
        self.persist()

    def clear_all(self) -> None:
        self.sessions = []
        self.persist()

    def update(self, session: Session, *, name: str | None = None, notes: str | None = None) -> None:
        if name is not None:
            session.name = name
        if notes is not None:
            session.notes = notes
        self.persist()

    def persist(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([s.to_json() for s in self.sessions], allow_nan=True))
            tmp.replace(self.path)
        except OSError:
            pass

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.sessions = []
        for d in raw:
            try:
                self.sessions.append(Session.from_json(d))
            except (TypeError, KeyError):
                continue
