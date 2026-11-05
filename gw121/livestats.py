"""Running statistics of the live readings (min, max, average, standard deviation), as zombu4's companion shows them.

Only numeric readings count (overload and blank readings are skipped). The values are the app's plot values, scaled to
one unit per mode (V for DCV whatever the range), so the statistics run on through range changes and restart when the
mode, AC/DC or the unit changes, or on Reset. Welford's method keeps the mean and the variance stable over long runs.
"""

from __future__ import annotations

import math
import time


class LiveStats:
    def __init__(self) -> None:
        self.key: str | None = None
        self.unit = ""
        self.reset()

    def reset(self) -> None:
        self.n = 0
        self.min: float | None = None
        self.max: float | None = None
        self._mean = 0.0
        self._m2 = 0.0
        self.started: float | None = None
        self.skipped = 0                    # overload / blank readings since the start

    def add(self, key: str, value: float, unit: str, now: float | None = None) -> bool:
        """Add one reading; True when the statistics restarted (other mode or unit)."""
        restarted = key != self.key or unit != self.unit
        if restarted:
            self.key, self.unit = key, unit
            self.reset()
        if not math.isfinite(value):
            self.skipped += 1
            return restarted
        if self.started is None:
            self.started = time.time() if now is None else now
        self.n += 1
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        d = value - self._mean
        self._mean += d / self.n
        self._m2 += d * (value - self._mean)
        return restarted

    @property
    def mean(self) -> float | None:
        return self._mean if self.n else None

    @property
    def std(self) -> float | None:
        """Sample standard deviation (n - 1), as the companion and a spreadsheet's STDEV."""
        return math.sqrt(self._m2 / (self.n - 1)) if self.n > 1 else None

    @property
    def peak_to_peak(self) -> float | None:
        return self.max - self.min if self.n else None
