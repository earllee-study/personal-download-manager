"""A shared token bucket used for speed limiting.

One bucket is shared by every segment worker, so ``--limit 5M`` means five
megabytes per second for the whole download rather than per connection.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate: int | None, burst_seconds: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._rate = float(rate) if rate else 0.0
        capacity = max(self._rate * burst_seconds, 64 * 1024) if rate else 0.0
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: int | None) -> None:
        with self._lock:
            self._rate = float(rate) if rate else 0.0
            self._capacity = max(self._rate, 64 * 1024) if rate else 0.0
            self._tokens = min(self._tokens, self._capacity)
            self._last = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def consume(self, count: int) -> None:
        """Block until ``count`` bytes may be sent."""
        if count <= 0 or self._rate <= 0:
            return
        remaining = count
        while remaining > 0:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens > 0:
                    take = min(remaining, self._tokens)
                    self._tokens -= take
                    remaining -= take
                    continue
                # Wait for at most one capacity window; re-check afterwards so
                # cancellation is never blocked for long.
                need = min(remaining, max(self._capacity, 1.0))
                wait = need / self._rate
            time.sleep(min(max(wait, 0.001), 0.25))
