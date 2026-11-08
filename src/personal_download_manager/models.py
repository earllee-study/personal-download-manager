"""Core data model shared by the engine, CLI and (future) GUI."""

from __future__ import annotations

import ssl
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A browser-like UA is the safest default: many CDNs serve a different (or
# broken) response for unknown agents.  Users can override with --user-agent.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MIB = 1024 * 1024


@dataclass(slots=True)
class DownloadConfig:
    """Everything needed to start one download.

    Defaults: up to 8 connections, 1 MiB minimum segment, identity encoding
    (so byte offsets stay valid), 5 retries.
    """

    url: str
    output: str | Path | None = None

    max_connections: int = 8
    min_segment_size: int = MIB
    min_split_size: int = MIB
    dynamic_segmentation: bool = True

    max_retries: int = 5
    retry_backoff: float = 1.0
    timeout: float = 30.0
    connect_timeout: float = 15.0
    read_timeout: float = 60.0

    speed_limit: int | None = None  # bytes/second, global for this download

    user_agent: str = DEFAULT_USER_AGENT
    headers: dict[str, str] = field(default_factory=dict)
    referer: str | None = None
    cookie: str | None = None
    auth: tuple[str, str] | None = None
    auth_type: str = "basic"  # "basic" | "digest"
    proxy: str | None = None
    # True/False for the default CA set, or an SSLContext for a custom trust
    # configuration (e.g. a private CA).  httpx passes contexts through as-is.
    verify: bool | ssl.SSLContext = True
    http2: bool = False
    http3: bool = False  # HTTP/3 over QUIC (needs the aioquic extra)

    # FTP/FTPS data connections are passive by default; active mode makes the
    # server connect back to a client listener (PORT/EPRT).  Some networks or
    # servers only allow one of the two.
    ftp_active: bool = False

    # SFTP (sftp://): password goes in auth/URL userinfo, key is a file path.
    sftp_key: str | None = None
    sftp_key_pass: str | None = None
    sftp_known_hosts: str | None = None
    sftp_auto_host_key: bool = False  # explicit opt-in, like StrictHostKeyChecking=no

    # Optional external ffmpeg used to mux separate DASH audio/video tracks.
    # Empty/None means "look for ffmpeg on PATH"; it is never bundled.
    ffmpeg_path: str | None = None

    resume: bool = True
    restart: bool = False
    overwrite: bool = False

    sha256: str | None = None

    show_progress: bool = True
    progress_interval: float = 0.25
    json_progress: bool = False
    quiet: bool = False
    log_level: str = "INFO"

    # Adaptive concurrency (AIMD). Off by default so benchmarks stay
    # repeatable; ``max_connections`` is the ceiling it ramps towards.
    adaptive: bool = False
    adaptive_start: int = 2
    adaptive_interval: float = 2.0

    # Extra URLs serving the identical file (same size / ETag), used to split
    # segments across hosts.
    mirrors: list[str] = field(default_factory=list)

    # Runtime-only shared policy (personal_download_manager.policy.Governor): per-host
    # connection caps, per-host limits/proxies and the global speed limit.
    # Not serialized; defaults to the process-wide governor when None.
    governor: Any | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("url is required")
        if not 1 <= self.max_connections <= 64:
            raise ValueError("max_connections must be between 1 and 64")
        if self.min_segment_size < 1:
            raise ValueError("min_segment_size must be positive")
        if self.min_split_size < 1:
            raise ValueError("min_split_size must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.retry_backoff < 0:
            raise ValueError("retry_backoff must be >= 0")
        if self.speed_limit is not None and self.speed_limit <= 0:
            raise ValueError("speed_limit must be positive")
        if self.auth_type not in ("basic", "digest"):
            raise ValueError("auth_type must be 'basic' or 'digest'")
        if self.adaptive_start < 1:
            raise ValueError("adaptive_start must be >= 1")
        if self.adaptive_interval <= 0:
            raise ValueError("adaptive_interval must be positive")
        cleaned = []
        for url in self.mirrors or []:
            url = str(url).strip()
            if url and url != self.url and url not in cleaned:
                cleaned.append(url)
        self.mirrors = cleaned

    @property
    def output_path(self) -> Path | None:
        return Path(self.output) if self.output is not None else None


@dataclass(slots=True)
class ProbeResult:
    """What we learned from the initial metadata request."""

    url: str
    final_url: str
    status: int
    total_size: int | None
    ranges: bool
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    content_disposition: str | None = None
    filename: str | None = None
    accept_ranges: str | None = None
    http_version: str | None = None  # "HTTP/2", "HTTP/1.1", ...
    cookies: dict[str, str] = field(default_factory=dict)

    def validator(self) -> str | None:
        """Value for If-Range: a strong ETag wins over Last-Modified."""
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return self.last_modified

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "total_size": self.total_size,
            "ranges": self.ranges,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_type": self.content_type,
            "content_disposition": self.content_disposition,
            "filename": self.filename,
            "accept_ranges": self.accept_ranges,
            "http_version": self.http_version,
        }


@dataclass(slots=True)
class DownloadResult:
    url: str
    final_url: str
    path: Path
    total_size: int
    downloaded: int
    elapsed: float
    avg_speed: float
    sha256: str | None
    range_supported: bool
    resumed: bool
    segments_created: int
    peak_connections: int
    retries: int
    # Connection budget when the transfer ended (differs from peak under AIMD).
    final_connections: int = 0
    # Extra files that belong to the same result (DASH dual-track without
    # ffmpeg keeps video and audio as two separate files).
    extra_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "result",
            "url": self.url,
            "final_url": self.final_url,
            "path": str(self.path),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "elapsed": round(self.elapsed, 3),
            "avg_speed": round(self.avg_speed, 1),
            "sha256": self.sha256,
            "range_supported": self.range_supported,
            "resumed": self.resumed,
            "segments_created": self.segments_created,
            "peak_connections": self.peak_connections,
            "final_connections": self.final_connections,
            "retries": self.retries,
            "extra_paths": list(self.extra_paths),
        }


@dataclass(slots=True)
class ProgressSnapshot:
    downloaded: int
    total: int | None
    speed: float
    eta: float | None
    connections: int
    segments: int
    elapsed: float
    percent: float | None
    state: str = "downloading"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "progress",
            "downloaded": self.downloaded,
            "total": self.total,
            "speed": round(self.speed, 1),
            "eta": round(self.eta, 2) if self.eta is not None else None,
            "connections": self.connections,
            "segments": self.segments,
            "elapsed": round(self.elapsed, 3),
            "percent": round(self.percent, 2) if self.percent is not None else None,
            "state": self.state,
        }


@dataclass
class Segment:
    """One contiguous byte range of the target file.

    ``pos`` is the next absolute file offset this segment still has to fetch.
    A worker is the only writer of ``pos``; every read/write of ``pos`` and
    ``end`` happens under ``lock`` because the scheduler may split a live
    segment by shrinking ``end``.
    """

    index: int
    start: int
    end: int
    pos: int = -1
    state: str = "pending"
    source: int = 0  # mirror index (0 = primary)
    lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.pos < 0:
            self.pos = self.start

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos)

    @property
    def done(self) -> bool:
        return self.pos >= self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "pos": self.pos,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            index=int(data["index"]),
            start=int(data["start"]),
            end=int(data["end"]),
            pos=int(data.get("pos", data["start"])),
            source=int(data.get("source", 0)),
        )


class DownloadState:
    """Thread-safe scheduling state for a multi-segment download.

    Responsibilities:
      * hand out pending segments
      * dynamically split the largest live segment when a worker goes idle
      * count bytes / active connections / retries
      * stop everyone on the first fatal error or user cancellation
    """

    def __init__(
        self,
        segments: list[Segment],
        total_size: int,
        max_connections: int,
        min_split_size: int,
        dynamic: bool,
        cancel_event: threading.Event,
        max_connections_limit: int | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self._condition = threading.Condition(self.lock)
        self.segments = list(segments)
        self.pending: deque[Segment] = deque(s for s in self.segments if not s.done)
        self.total_size = total_size
        self.max_connections = max_connections
        # AIMD may move max_connections between 1 and this ceiling.
        self.max_connections_limit = max_connections_limit or max_connections
        self.min_split_size = min_split_size
        self.dynamic = dynamic
        self.cancel_event = cancel_event

        self.downloaded = sum(max(0, s.pos - s.start) for s in self.segments)
        self.active = 0
        self.completed = sum(1 for s in self.segments if s.done)
        self.segments_created = len(self.segments)
        self.peak_active = 0
        self.retries = 0
        self.pressure = 0  # count of 429/503 responses (AIMD congestion signal)
        self.last_pressure_status = 0
        self.error: BaseException | None = None
        self._next_index = max((s.index for s in self.segments), default=-1) + 1

    # -- scheduling -----------------------------------------------------
    def next_segment(self) -> Segment | None:
        """Take the next piece of work, waiting while the budget is full.

        Returns ``None`` only when the download is cancelled/failed or when no
        further work can appear.  A worker that is over the current
        connection cap waits here, so an AIMD increase can wake it up without
        spawning new threads.
        """
        with self._condition:
            while True:
                if self.cancel_event.is_set() or self.error is not None:
                    return None

                if self.active < self.max_connections:
                    while self.pending:
                        seg = self.pending.popleft()
                        if not seg.done:
                            seg.state = "active"
                            self.active += 1
                            self.peak_active = max(self.peak_active, self.active)
                            return seg
                    if self.dynamic and self.completed:
                        tail = self._try_split()
                        if tail is not None:
                            return tail

                if not self.pending and not self._can_split():
                    return None  # nothing new can ever start
                # Either at the connection cap or racing another worker.
                self._condition.wait(0.1)

    def _can_split(self) -> bool:
        if not self.dynamic or self.completed == 0:
            return False
        threshold = 2 * self.min_split_size
        return any(
            seg.state == "active" and seg.remaining >= threshold
            for seg in self.segments
        )

    def _try_split(self) -> Segment | None:
        # Split the segment with the most work left.  The split point is
        # clamped to the live cursor, so the original worker never has to
        # re-download bytes it already wrote.
        best: Segment | None = None
        best_remaining = 2 * self.min_split_size - 1
        for seg in self.segments:
            if seg.state == "active" and seg.remaining > best_remaining:
                best, best_remaining = seg, seg.remaining
        if best is None:
            return None

        with best.lock:
            if best.state != "active":
                return None
            remaining = best.end - best.pos
            if remaining < 2 * self.min_split_size:
                return None
            split_at = best.pos + remaining // 2
            tail = Segment(
                index=self._next_index,
                start=split_at,
                end=best.end,
                pos=split_at,
                state="active",
                source=best.source,
            )
            self._next_index += 1
            best.end = split_at
            self.segments.append(tail)
            self.segments_created += 1
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            return tail

    def set_max_connections(self, value: int) -> int:
        """Change the live connection budget (AIMD); wakes waiting workers."""
        with self._condition:
            new_value = max(1, min(self.max_connections_limit, int(value)))
            if new_value != self.max_connections:
                self.max_connections = new_value
                self._condition.notify_all()
            return self.max_connections

    def note_pressure(self, status: int) -> None:
        """Record a 429/503; the adaptive controller reacts on its next tick."""
        with self.lock:
            self.pressure += 1
            self.last_pressure_status = status

    def segment_finished(self, seg: Segment) -> None:
        with self._condition:
            if seg.state == "active":
                self.active = max(0, self.active - 1)
            seg.state = "done"
            self.completed += 1
            # Defensive: keep the counter exact even after an odd split race.
            self.downloaded = max(
                self.downloaded, sum(max(0, s.pos - s.start) for s in self.segments)
            )
            self._condition.notify_all()

    def release(self, seg: Segment) -> None:
        """Return a segment to the pending queue after cancellation/error."""
        with self._condition:
            if seg.state == "active":
                self.active = max(0, self.active - 1)
            if not seg.done:
                seg.state = "pending"
            self._condition.notify_all()

    def add_bytes(self, count: int) -> None:
        with self.lock:
            self.downloaded += count

    def add_retry(self) -> None:
        with self.lock:
            self.retries += 1

    def fail(self, exc: BaseException) -> None:
        with self._condition:
            if self.error is None:
                self.error = exc
            self._condition.notify_all()
        self.cancel_event.set()

    def all_done(self) -> bool:
        with self.lock:
            return all(s.done for s in self.segments)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [s.to_dict() for s in sorted(self.segments, key=lambda s: s.index)]

    def counters(self) -> tuple[int, int, int, int]:
        """(downloaded, active, segments_created, retries) under one lock."""
        with self.lock:
            return (
                self.downloaded,
                self.active,
                self.segments_created,
                self.retries,
            )

    def progress_counters(self) -> tuple[int, int, int]:
        """(downloaded, active connections, live segments) for the reporter."""
        with self.lock:
            return self.downloaded, self.active, len(self.segments)
