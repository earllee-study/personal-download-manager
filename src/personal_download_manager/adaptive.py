"""AIMD connection control (additive increase, multiplicative decrease).

Fixed connection counts are a guess: 8 is fast against a per-connection
throttle and abusive against a server that answers 429/503.  This controller
starts small, adds one connection for every ``interval`` seconds of healthy
progress, and halves the budget when the server pushes back.

The class is deliberately pure (no threads, no clock): callers pass time and
progress deltas, which keeps the policy unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AimdController:
    minimum: int = 1
    maximum: int = 8
    start: int = 2
    interval: float = 2.0   # seconds of healthy progress per +1 connection
    cooldown: float = 3.0   # seconds without increases after a decrease

    _count: int = field(init=False, default=0)
    _last_change: float | None = field(init=False, default=None)
    _last_pressure: float = field(init=False, default=float("-inf"))

    def __post_init__(self) -> None:
        self.maximum = max(1, int(self.maximum))
        self.minimum = max(1, min(int(self.minimum), self.maximum))
        self.start = max(self.minimum, min(self.maximum, int(self.start)))
        self.interval = max(0.05, float(self.interval))
        self.cooldown = max(0.0, float(self.cooldown))
        self._count = self.start

    @property
    def count(self) -> int:
        return self._count

    def observe(self, now: float, downloaded: int, elapsed: float) -> int:
        """Record healthy progress; returns the connection budget to use."""
        if downloaded <= 0 or elapsed <= 0:
            return self._count
        if self._last_change is None:
            self._last_change = now
            return self._count
        if self._count >= self.maximum:
            return self._count
        if now - self._last_pressure < self.cooldown:
            return self._count
        if now - self._last_change >= self.interval:
            self._count += 1
            self._last_change = now
        return self._count

    def on_pressure(self, now: float) -> int:
        """A 429/503 arrived: halve, and stop increasing for the cooldown."""
        self._last_pressure = now
        self._count = max(self.minimum, self._count // 2)
        self._last_change = now
        return self._count
