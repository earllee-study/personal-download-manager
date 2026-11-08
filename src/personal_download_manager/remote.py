"""Shared plumbing for single-stream remote protocols (FTP and SFTP).

Both protocols hand the driver the same things:

* metadata (remote name, size, modification time) for progress and for
  detecting a changed file between runs,
* a ``read_from(offset, total, sink)`` callable that connects, seeks and
  streams chunks,
* errors expressed with the engine's existing hierarchy.

The driver here owns everything protocol-independent: output naming, the
resume journal (``.pdm.json``) and part file, retries with backoff, speed
limits, per-host connection accounting, cancellation, SHA-256 and the final
rename.  Keeping that in one place is why FTP and SFTP each stay small.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import (
    Cancelled,
    ChecksumMismatch,
    ContentChanged,
    DiskError,
    FatalError,
    IncompleteTransfer,
    RetryableError,
)
from .headers import filename_from_url, sanitize_filename, unique_path
from .journal import JournalStore
from .models import DownloadConfig, DownloadResult
from .policy import default_governor
from .progress import SimpleProgress
from .progress import ProgressReporter
from .ratelimit import TokenBucket

JOURNAL_INTERVAL = 1.5


class CancelRelay:
    """Turn "cancel was requested" into ``close()`` on live protocol sockets.

    The engine's cancel event is set by the GUI thread, but ``ftplib`` and
    paramiko block inside ``recv``/``read``.  A tiny watcher closes the
    registered sockets, which both unblocks the reader immediately and keeps
    the pause latency of FTP/SFTP on par with HTTP.
    """

    def __init__(self, event: threading.Event | None) -> None:
        self._event = event
        self._lock = threading.Lock()
        self._closers: list[Callable[[], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "CancelRelay":
        if self._event is not None and self._thread is None:
            self._thread = threading.Thread(
                target=self._watch, name="remote-cancel", daemon=True
            )
            self._thread.start()
        return self

    def _watch(self) -> None:
        assert self._event is not None
        while not self._stop.wait(0.1):
            if self._event.is_set():
                self.close()
                return

    def is_set(self) -> bool:
        return self._event is not None and self._event.is_set()

    def set(self) -> None:
        """Set the underlying event and close registered sockets (state.fail)."""
        if self._event is not None:
            self._event.set()
        self.close()

    def wait(self, timeout: float | None = None) -> bool:
        if self._event is None:
            time.sleep(timeout or 0)
            return False
        return self._event.wait(timeout)

    def register(self, closer: Callable[[], None]) -> None:
        with self._lock:
            self._closers.append(closer)
        if self.is_set():
            self.close()

    def unregister(self, closer: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._closers.remove(closer)
            except ValueError:
                pass

    def close(self) -> None:
        with self._lock:
            closers = list(self._closers)
            self._closers.clear()
        for closer in closers:
            try:
                closer()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self.close()


def display_url(url: str) -> str:
    """Hide user:password before putting a remote URL in an error message."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"***@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class RemoteLocation:
    scheme: str
    host: str
    port: int
    username: str | None
    password: str | None
    path: str


@dataclass(frozen=True)
class RemoteMeta:
    filename: str
    total_size: int | None
    mtime: str | None = None


def parse_remote_url(
    config: DownloadConfig, *, default_port: int, schemes: tuple[str, ...]
) -> RemoteLocation:
    """Split a remote URL; URL userinfo wins over ``config.auth``."""
    parts = urlsplit(config.url)
    scheme = (parts.scheme or "").lower()
    if scheme not in schemes:
        raise FatalError(f"unsupported remote URL scheme {scheme!r}")
    host = parts.hostname
    if not host:
        raise FatalError(f"remote URL has no host: {display_url(config.url)}")
    port = parts.port or default_port
    username = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    if username is None and config.auth:
        username, password = config.auth
    path = unquote(parts.path) or "/"
    for value in (username, password, path):
        if value and ("\r" in value or "\n" in value):
            # FTP sends the path/user as a command line; a decoded CR/LF would
            # let a URL inject extra commands.
            raise FatalError("remote URL contains an illegal newline")
    return RemoteLocation(
        scheme=scheme,
        host=host,
        port=port,
        username=username or None,
        password=password or None,
        path=path,
    )


def remote_name(config: DownloadConfig, path: str) -> str:
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    return sanitize_filename(tail or filename_from_url(config.url) or "download")


def reject_proxy(config: DownloadConfig, scheme: str) -> None:
    """FTP/SFTP have no HTTP proxy support; fail instead of silently ignoring."""
    if config.proxy:
        raise FatalError(
            f"{scheme.upper()} through an HTTP proxy is not supported"
        )


def _output_and_part(
    config: DownloadConfig, name: str
) -> tuple[Path, Path, Path]:
    raw = config.output
    if raw is None:
        output = Path.cwd() / name
    else:
        text = str(raw)
        candidate = Path(raw)
        if candidate.exists() and candidate.is_dir():
            output = candidate / name
        elif text.endswith(("/", "\\")):
            candidate.mkdir(parents=True, exist_ok=True)
            output = candidate / name
        else:
            output = candidate
    return output, output.with_name(output.name + ".pdm.part"), output.with_name(
        output.name + ".pdm.json"
    )


def resolve_targets(
    config: DownloadConfig, name: str
) -> tuple[Path, Path, JournalStore]:
    """Final output / part / journal paths, avoiding clobbering a finished file."""
    output, part, journal_path = _output_and_part(config, name)
    if not config.overwrite and output.exists() and not part.exists():
        output = unique_path(output)
        part = output.with_name(output.name + ".pdm.part")
        journal_path = output.with_name(output.name + ".pdm.json")
    return output, part, JournalStore(journal_path)


def journal_matches(
    entry: dict | None, mode: str, config: DownloadConfig, meta: RemoteMeta
) -> bool:
    return entry is not None and _matches(entry, mode, config, meta)


def _matches(entry: dict, mode: str, config: DownloadConfig, meta: RemoteMeta) -> bool:
    if entry.get("mode") != mode:
        return False
    if entry.get("url") != config.url:
        return False
    if entry.get("total_size") != meta.total_size:
        return False
    if meta.mtime is not None and entry.get("mtime") not in (None, meta.mtime):
        return False
    return True


def _prepare_resume(
    config: DownloadConfig,
    *,
    mode: str,
    meta: RemoteMeta,
    part: Path,
    journal: JournalStore,
) -> tuple[int, bool]:
    """Return ``(offset, resumed)``; discard a journal that no longer matches."""
    if not config.resume or config.restart:
        journal.delete()
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return 0, False
    entry = journal.load()
    if entry is None or not part.exists():
        return 0, False
    if not _matches(entry, mode, config, meta):
        journal.delete()
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return 0, False
    size = part.stat().st_size
    try:
        pos = int(entry.get("pos", 0))
    except (TypeError, ValueError):
        pos = 0
    pos = max(0, min(pos, size))
    if meta.total_size is not None:
        if size > meta.total_size:
            return 0, False
        pos = min(pos, meta.total_size)
    return pos, pos > 0


def _save_journal(
    journal: JournalStore,
    *,
    mode: str,
    config: DownloadConfig,
    meta: RemoteMeta,
    output: Path,
    part: Path,
    pos: int,
) -> None:
    journal.save(
        {
            "mode": mode,
            "url": config.url,
            "total_size": meta.total_size,
            "mtime": meta.mtime,
            "output": str(output),
            "part": part.name,
            "pos": pos,
        }
    )


def save_multi_journal(
    journal: JournalStore,
    *,
    mode: str,
    config: DownloadConfig,
    meta: RemoteMeta,
    output: Path,
    part: Path,
    segments: list[dict],
) -> None:
    journal.save(
        {
            "mode": mode,
            "url": config.url,
            "total_size": meta.total_size,
            "mtime": meta.mtime,
            "output": str(output),
            "part": part.name,
            "segments": segments,
        }
    )


def finalize_download(
    config: DownloadConfig,
    *,
    started: float,
    output: Path,
    part: Path,
    journal: JournalStore,
    total: int | None,
    downloaded: int,
    segments_created: int,
    peak_connections: int,
    retries: int,
    resumed: bool,
    final_connections: int = 1,
    range_supported: bool = False,
) -> DownloadResult:
    """Verify size/SHA, rename part to output and delete the journal."""
    try:
        size = part.stat().st_size
    except OSError as exc:
        raise DiskError(f"cannot stat {part}: {exc}") from exc
    if total is not None and size != total:
        raise IncompleteTransfer(f"part file is {size} bytes, expected {total}")

    digest: str | None = None
    if config.sha256:
        digest = _sha256(part)
        if digest.lower() != config.sha256.lower():
            raise ChecksumMismatch(config.sha256, digest)

    try:
        if output.exists() and not config.overwrite:
            output = unique_path(output)
        os.replace(part, output)
    except OSError as exc:
        raise DiskError(f"cannot move {part} to {output}: {exc}") from exc
    journal.delete()

    elapsed = max(1e-9, time.monotonic() - started)
    return DownloadResult(
        url=config.url,
        final_url=config.url,
        path=output,
        total_size=total if total is not None else size,
        downloaded=downloaded,
        elapsed=elapsed,
        avg_speed=downloaded / elapsed,
        sha256=digest,
        range_supported=range_supported,
        resumed=resumed,
        segments_created=segments_created,
        peak_connections=peak_connections,
        retries=retries,
        final_connections=final_connections,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _retry_delay(config: DownloadConfig, attempt: int, exc: RetryableError) -> float:
    if exc.retry_after is not None:
        return max(0.05, float(exc.retry_after))
    return min(30.0, max(0.0, config.retry_backoff) * (2 ** max(0, attempt - 1)))


def _sleep(delay: float, cancel_event: threading.Event | None) -> None:
    deadline = time.monotonic() + max(0.0, delay)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("download cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if cancel_event is not None:
            cancel_event.wait(min(remaining, 0.1))
        else:
            time.sleep(min(remaining, 0.1))


def _open_part(part: Path, pos: int):
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        if pos > 0 and part.exists():
            handle = open(part, "r+b")
            handle.seek(pos)
        else:
            handle = open(part, "wb")
        return handle
    except OSError as exc:
        raise DiskError(f"cannot open {part}: {exc}") from exc


def single_stream_download(
    config: DownloadConfig,
    *,
    mode: str,
    meta: RemoteMeta,
    read_from: Callable[[int, int | None, Callable[[bytes], None]], None],
    cancel_event: threading.Event | None = None,
    progress_callback=None,
    range_supported: bool = False,
) -> DownloadResult:
    """Download one remote file sequentially, resuming when possible."""
    started = time.monotonic()
    name = sanitize_filename(meta.filename or "download")
    output, part, journal = resolve_targets(config, name)

    pos, resumed = _prepare_resume(
        config, mode=mode, meta=meta, part=part, journal=journal
    )
    start_pos = pos
    total = meta.total_size

    if total == 0:
        if output.exists() and not config.overwrite:
            output = unique_path(output)
            part = output.with_name(output.name + ".pdm.part")
            journal = JournalStore(
                output.with_name(output.name + ".pdm.json")
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"")
        part.unlink(missing_ok=True)
        journal.delete()
        elapsed = max(1e-9, time.monotonic() - started)
        return DownloadResult(
            url=config.url,
            final_url=config.url,
            path=output,
            total_size=0,
            downloaded=0,
            elapsed=elapsed,
            avg_speed=0.0,
            sha256=hashlib.sha256(b"").hexdigest() if config.sha256 else None,
            range_supported=range_supported,
            resumed=False,
            segments_created=0,
            peak_connections=0,
            retries=0,
            final_connections=0,
        )

    governor = config.governor or default_governor()
    policy = governor.resolve(config.url)
    if policy.proxy:
        raise FatalError(
            f"{mode.upper()} through an HTTP proxy is not supported "
            f"(host rule for {display_url(config.url)})"
        )
    limits = [value for value in (config.speed_limit, policy.speed_limit) if value]
    bucket = TokenBucket(min(limits) if limits else None)

    progress = SimpleProgress()
    progress.downloaded = pos
    reporter = ProgressReporter(total, progress, config, progress_callback)
    reporter.start()
    last_journal = time.monotonic()
    retries = 0
    attempt = 0
    final_state = "done"

    def sink(chunk: bytes) -> None:
        nonlocal pos, last_journal
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("download cancelled")
        if total is not None:
            remaining = total - pos
            if remaining <= 0:
                return
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
        if not chunk:
            return
        try:
            handle.write(chunk)
        except OSError as exc:
            raise DiskError(f"write failed at offset {pos}: {exc}") from exc
        pos += len(chunk)
        progress.add(len(chunk))
        governor.consume(len(chunk))
        bucket.consume(len(chunk))
        now = time.monotonic()
        if now - last_journal >= JOURNAL_INTERVAL:
            handle.flush()
            _save_journal(
                journal,
                mode=mode,
                config=config,
                meta=meta,
                output=output,
                part=part,
                pos=pos,
            )
            last_journal = now

    handle = _open_part(part, pos)
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled("download cancelled")
            if total is not None and pos >= total:
                break
            before = pos
            try:
                acquired = governor.acquire(config.url, cancel_event)
                if not acquired:
                    raise Cancelled("download cancelled")
                try:
                    read_from(pos, total, sink)
                finally:
                    governor.release(config.url)
                if total is not None and pos < total:
                    raise IncompleteTransfer(
                        f"connection closed at {pos} of {total} bytes"
                    )
                break
            except RetryableError as exc:
                retries += 1
                if pos > before:
                    attempt = 0  # progress was made; reset the failure streak
                attempt += 1
                if attempt > config.max_retries:
                    final_state = "failed"
                    raise FatalError(
                        f"{mode.upper()} transfer failed after "
                        f"{attempt - 1} retries: {exc}"
                    ) from exc
                handle.flush()
                _save_journal(
                    journal,
                    mode=mode,
                    config=config,
                    meta=meta,
                    output=output,
                    part=part,
                    pos=pos,
                )
                _sleep(_retry_delay(config, attempt, exc), cancel_event)
    except Cancelled:
        final_state = "cancelled"
        raise
    except BaseException:
        if final_state == "done":
            final_state = "failed"
        raise
    finally:
        try:
            handle.flush()
            if final_state != "done":
                # On success the journal is about to be deleted; saving it
                # would only add an fsync to the common path.
                _save_journal(
                    journal,
                    mode=mode,
                    config=config,
                    meta=meta,
                    output=output,
                    part=part,
                    pos=pos,
                )
            handle.close()
        except OSError:
            pass
        reporter.stop(final_state)

    # -- success ---------------------------------------------------------
    transferred = max(0, pos - start_pos)
    return finalize_download(
        config,
        started=started,
        output=output,
        part=part,
        journal=journal,
        total=total,
        downloaded=transferred,
        segments_created=1,
        peak_connections=1,
        retries=retries,
        resumed=resumed,
        final_connections=1,
        range_supported=range_supported,
    )


def check_remote_change(
    expected: RemoteMeta,
    current_size: int | None,
    current_mtime: str | None,
) -> None:
    """Raise if the remote file changed while we were downloading it."""
    if (
        expected.total_size is not None
        and current_size is not None
        and current_size != expected.total_size
    ):
        raise ContentChanged(
            f"remote size changed from {expected.total_size} to {current_size}"
        )
    if (
        expected.mtime
        and current_mtime
        and current_mtime != expected.mtime
    ):
        raise ContentChanged(
            f"remote modification time changed from {expected.mtime} "
            f"to {current_mtime}"
        )
