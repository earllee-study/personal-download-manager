"""Shared helpers for segmented streaming downloads (HLS and DASH).

Both formats boil down to: fetch a manifest, download N segment URLs (some
with byte ranges, some with an initialization segment) concurrently, then
concatenate them in order.  The concurrency, retry, governor, rate-limit and
progress logic is identical, so it lives here once.

These helpers never decide what a segment is; callers hand over concrete
``(url, byterange)`` tasks.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .errors import Cancelled, FatalError, RetryableError
from .headers import parse_retry_after
from .models import DownloadConfig
from .ratelimit import TokenBucket


@dataclass
class StreamProgress:
    """Thread-safe counters shared by segment workers and the reporter."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    peak: int = 0
    retries: int = 0
    downloaded: int = 0
    completed: int = 0
    duration_done: float = 0.0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)

    def add_retry(self) -> None:
        with self.lock:
            self.retries += 1

    def add_bytes(self, count: int) -> None:
        with self.lock:
            self.downloaded += count

    def segment_done(self, duration: float = 0.0) -> None:
        with self.lock:
            self.completed += 1
            self.duration_done += max(0.0, duration)

    def snapshot(self) -> tuple[int, int, int, float]:
        with self.lock:
            return self.downloaded, self.active, self.completed, self.duration_done


def check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("download cancelled")


def report_loop(
    progress: StreamProgress,
    total_segments: int,
    total_duration: float,
    started: float,
    callback: Callable[[dict], None],
    stop: threading.Event,
) -> None:
    """Report stream progress.

    Manifests do not carry byte sizes, so the fraction comes from declared
    segment durations (or completed segments when durations are missing); the
    ETA is extrapolated from the wall-clock rate of the same fraction.
    """
    last_bytes = 0
    last_time = time.monotonic()
    while not stop.wait(0.25):
        downloaded, active, completed, duration_done = progress.snapshot()
        now = time.monotonic()
        elapsed = now - last_time
        speed = (downloaded - last_bytes) / elapsed if elapsed > 0 else 0.0

        if total_duration > 0:
            fraction = min(1.0, duration_done / total_duration)
        elif total_segments > 0:
            fraction = min(1.0, completed / total_segments)
        else:
            fraction = 0.0
        overall = max(0.0, now - started)
        eta = (
            overall * (1.0 - fraction) / fraction
            if fraction > 0.02 and fraction < 1.0
            else None
        )
        try:
            callback(
                {
                    "downloaded": downloaded,
                    "total": None,
                    "speed": speed,
                    "eta": eta,
                    "fraction": fraction,
                    "connections": active,
                    "segments": total_segments,
                    "segments_completed": completed,
                }
            )
        except Exception:  # a broken UI callback must not kill the download
            pass
        last_bytes, last_time = downloaded, now


def download_stream_segment(
    client: httpx.Client,
    url: str,
    byterange: tuple[int, int] | None,
    target: Path,
    config: DownloadConfig,
    bucket: TokenBucket,
    governor,
    progress: StreamProgress,
    cancel_event: threading.Event | None = None,
    duration: float = 0.0,
) -> None:
    """Download one segment (optionally a byte range) with retries."""
    headers = {}
    if byterange is not None:
        length, offset = byterange
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    attempt = 0
    while True:
        check_cancel(cancel_event)
        try:
            if not governor.acquire(url, cancel_event):
                raise Cancelled("download cancelled")
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code not in (200, 206):
                        if response.status_code in (429, 503):
                            raise RetryableError(
                                f"HTTP {response.status_code} for {url}",
                                parse_retry_after(response.headers.get("retry-after")),
                            )
                        raise FatalError(
                            f"HTTP {response.status_code} for {url}"
                        )
                    with open(target, "wb") as handle:
                        for chunk in response.iter_bytes():
                            check_cancel(cancel_event)
                            if not chunk:
                                continue
                            handle.write(chunk)
                            progress.add_bytes(len(chunk))
                            governor.consume(len(chunk))
                            bucket.consume(len(chunk))
            finally:
                governor.release(url)
            if byterange is not None and target.stat().st_size != byterange[0]:
                raise RetryableError(
                    f"short range for {url}: got {target.stat().st_size} "
                    f"of {byterange[0]}"
                )
            progress.segment_done(duration)
            return
        except Cancelled:
            raise
        except (RetryableError, httpx.TransportError, httpx.TimeoutException) as exc:
            attempt += 1
            progress.add_retry()
            if attempt > config.max_retries:
                raise FatalError(
                    f"segment {url} failed after {attempt - 1} retries: {exc}"
                ) from exc
            delay = getattr(exc, "retry_after", None)
            if delay is None:
                delay = min(30.0, config.retry_backoff * (2 ** (attempt - 1)))
            if delay <= 0:
                delay = 0.05
            time.sleep(delay)
