"""Human and machine readable progress reporting.

The reporter samples counters instead of being pushed an event per chunk, so
it adds no lock contention to the hot download path.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from typing import Callable, Protocol

from .headers import format_eta, format_size, format_speed
from .models import DownloadConfig, ProgressSnapshot


class ProgressSource(Protocol):
    def progress_counters(self) -> tuple[int, int, int]:  # downloaded, active, segments
        ...


ProgressCallback = Callable[[ProgressSnapshot], None]


class ProgressReporter:
    def __init__(
        self,
        total: int | None,
        source: ProgressSource,
        config: DownloadConfig,
        callback: ProgressCallback | None = None,
    ) -> None:
        self.total = total
        self.source = source
        self.config = config
        self.callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = time.monotonic()
        self._last_time = self._start
        self._last_bytes = 0
        self._speed: float | None = None
        self._last_render = 0.0
        self._line_len = 0
        self._last_plain = 0.0
        self._final: ProgressSnapshot | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        if not (
            self.config.show_progress
            or self.config.json_progress
            or self.callback
        ):
            return
        self._thread = threading.Thread(
            target=self._run, name="pdm-progress", daemon=True
        )
        self._thread.start()

    def stop(self, state: str = "done") -> ProgressSnapshot:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        snapshot = self.snapshot(state)
        self._render(snapshot, final=True)
        return snapshot

    # -- sampling -------------------------------------------------------
    def snapshot(self, state: str = "downloading") -> ProgressSnapshot:
        now = time.monotonic()
        downloaded, active, segments = self.source.progress_counters()
        dt = now - self._last_time
        if dt > 0:
            instant = max(0.0, (downloaded - self._last_bytes) / dt)
            if self._speed is None:
                self._speed = instant
            else:
                alpha = 1.0 - math.exp(-dt / 1.5)
                self._speed += alpha * (instant - self._speed)
            self._last_time = now
            self._last_bytes = downloaded
        percent = None
        if self.total and self.total > 0:
            percent = min(100.0, downloaded * 100.0 / self.total)
        eta = None
        if self.total and self._speed and self._speed > 1:
            eta = max(0.0, (self.total - downloaded) / self._speed)
        return ProgressSnapshot(
            downloaded=downloaded,
            total=self.total,
            speed=self._speed or 0.0,
            eta=eta,
            connections=active,
            segments=segments,
            elapsed=now - self._start,
            percent=percent,
            state=state,
        )

    def _run(self) -> None:
        interval = max(0.05, self.config.progress_interval)
        while not self._stop.wait(interval):
            snapshot = self.snapshot()
            self._render(snapshot)

    # -- output ---------------------------------------------------------
    def _render(self, snapshot: ProgressSnapshot, final: bool = False) -> None:
        if self.callback is not None:
            try:
                self.callback(snapshot)
            except Exception:
                pass
        if self.config.json_progress:
            # JSON stays on stdout so benchmarks/CI can parse one object per line.
            print(json.dumps(snapshot.to_dict(), ensure_ascii=False), flush=True)
            return
        if not self.config.show_progress or self.config.quiet:
            return

        downloaded = format_size(snapshot.downloaded)
        total = format_size(snapshot.total) if snapshot.total is not None else "?"
        percent = f"{snapshot.percent:5.1f}%" if snapshot.percent is not None else "  ?  "
        line = (
            f"{percent}  {downloaded}/{total}  "
            f"{format_speed(snapshot.speed)}  "
            f"conns {snapshot.connections:>2}  "
            f"parts {snapshot.segments:>3}  "
            f"ETA {format_eta(snapshot.eta)}"
        )
        if final:
            line = f"{line}  [{snapshot.state}]"

        now = time.monotonic()
        tty = sys.stderr.isatty()
        if tty:
            pad = max(0, self._line_len - len(line))
            sys.stderr.write("\r" + line + " " * pad)
            if final:
                sys.stderr.write("\n")
            sys.stderr.flush()
            self._line_len = len(line)
        else:
            # Do not flood redirected logs: one line every five seconds.
            if final or now - self._last_plain >= 5.0:
                print(line, file=sys.stderr, flush=True)
                self._last_plain = now


class SimpleProgress:
    """Minimal ProgressSource for single-connection downloads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.downloaded = 0
        self.active = 1
        self.segments = 1

    def add(self, count: int) -> None:
        with self.lock:
            self.downloaded += count

    def set_active(self, active: int) -> None:
        with self.lock:
            self.active = active

    def progress_counters(self) -> tuple[int, int, int]:
        with self.lock:
            return self.downloaded, self.active, self.segments
