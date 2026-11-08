"""The download engine.

Design notes:

* Probe first, then partition the file into byte ranges.
* One TCP/TLS connection per range, each with its own part of the file.
* Dynamic segmentation: when a worker finishes early, split the live segment
  with the most work left and keep the connection count at the configured
  maximum.  The split point is clamped to the victim's live cursor so no byte
  is downloaded twice.
* Every range request uses ``Accept-Encoding: identity`` and, when available,
  ``If-Range`` so a changed ETag can never silently corrupt the file.
* All writes are positional into one preallocated file; there is no expensive
  "merge the chunks" pass at the end.
* A JSON journal records each segment cursor after the bytes hit the file, so
  a crash can only cause re-download, never data loss.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .adaptive import AimdController
from .client import apply_policy, make_client, redact_url
from .errors import (
    Cancelled,
    ChecksumMismatch,
    ContentChanged,
    DiskError,
    FatalError,
    HTTPStatusError,
    PdmError,
    IncompleteTransfer,
    ProtocolError,
    RangeNotSupported,
    RetryableError,
)
from .headers import (
    parse_content_range,
    parse_retry_after,
    sanitize_filename,
    unique_path,
)
from .journal import JournalStore
from .mirror import MirrorPool, mirrors_compatible
from .models import (
    DownloadConfig,
    DownloadResult,
    DownloadState,
    ProbeResult,
    ProgressSnapshot,
    Segment,
)
from .policy import ResolvedPolicy, default_governor
from .probe import probe as probe_url
from .progress import ProgressCallback, ProgressReporter, SimpleProgress
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_PROGRESS_JOURNAL_INTERVAL = 1.5


class Downloader:
    def __init__(
        self,
        config: DownloadConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.callback = progress_callback
        self.cancel_event = threading.Event()
        self._governor = config.governor or default_governor()
        self._policy = ResolvedPolicy()
        self._request_config = config
        self._mirrors: MirrorPool | None = None
        # The per-download bucket is layered on top of the governor's global
        # bucket: both must allow the bytes before they are read.
        self._limiter = TokenBucket(config.speed_limit)
        self._started_at = 0.0
        self._resumed = False
        self._probe: ProbeResult | None = None
        self._output: Path | None = None
        self._part: Path | None = None
        self._journal: JournalStore | None = None
        self._clients_lock = threading.Lock()
        self._clients: set[httpx.Client] = set()
        self._last_snapshot: ProgressSnapshot | None = None
        self._apply_policy(config.url)

    # ------------------------------------------------------------------
    # host policy
    # ------------------------------------------------------------------
    def _apply_policy(self, url: str) -> None:
        """Resolve per-host rules for ``url`` (initial URL, then redirects)."""
        policy = self._governor.resolve(url)
        self._policy = policy
        self._request_config = self._request_config_for(url, policy)
        limits = [
            value
            for value in (self.config.speed_limit, policy.speed_limit)
            if value
        ]
        self._limiter.set_rate(min(limits) if limits else None)

    def _request_config_for(
        self, url: str, policy: ResolvedPolicy | None = None
    ) -> DownloadConfig:
        """Task config with the host rule's proxy/UA/Cookie/auth applied."""
        if policy is None:
            policy = self._governor.resolve(url)
        return apply_policy(self.config, policy)

    def _effective_connections(self) -> int:
        if self._mirrors is not None and self._mirrors.count > 1:
            # The per-host cap is enforced per mirror by the governor, so the
            # total budget must span hosts rather than inherit the primary's.
            return self.config.max_connections
        cap = self._policy.connections
        if cap is None:
            return self.config.max_connections
        return max(1, min(self.config.max_connections, cap))

    def _transfer_host(self) -> str:
        probe = self._probe
        return (probe.final_url if probe and probe.final_url else self.config.url)

    # ------------------------------------------------------------------
    # mirrors
    # ------------------------------------------------------------------
    def _build_mirrors(self, primary: ProbeResult) -> MirrorPool | None:
        """Probe each mirror and keep only those serving identical bytes."""
        accepted: list[ProbeResult] = []
        for url in self.config.mirrors:
            try:
                # Each mirror is its own site: apply its host rule to the probe
                # too, so a mirror that needs its own Cookie/auth is accepted
                # instead of being discarded as unreachable/forbidden.
                candidate = probe_url(
                    replace(self._request_config_for(url), url=url)
                )
            except PdmError as exc:
                log.warning("mirror %s unusable: %s", url, exc)
                continue
            if not mirrors_compatible(primary, candidate):
                log.warning(
                    "mirror %s does not match the primary file "
                    "(size %s vs %s, etag %s vs %s)",
                    url,
                    candidate.total_size,
                    primary.total_size,
                    candidate.etag,
                    primary.etag,
                )
                continue
            accepted.append(candidate)
        log.info("mirrors: %d accepted of %d", len(accepted), len(self.config.mirrors))
        return MirrorPool(primary, accepted)

    def _segment_source(
        self, segment: Segment, probe: ProbeResult
    ) -> tuple[str, ProbeResult]:
        """URL and probe for the mirror serving ``segment``.

        The probe carries that mirror's ``Set-Cookie`` values, so segment
        requests can authenticate against each mirror's own site rules.
        """
        if self._mirrors is not None:
            entry = self._mirrors.entry_for(segment)
            return entry.url, entry.probe
        return probe.final_url or self.config.url, probe

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def run(self) -> DownloadResult:
        scheme = urlsplit(self.config.url).scheme.lower()
        if scheme in ("ftp", "ftps"):
            from .ftp import download_ftp

            return download_ftp(self.config, self.cancel_event, self.callback)
        if scheme == "sftp":
            from .sftp import download_sftp

            return download_sftp(self.config, self.cancel_event, self.callback)

        self._started_at = time.monotonic()
        probe = probe_url(self._request_config)
        self._probe = probe
        # A redirect can move the transfer to another host, which may have its
        # own connection cap, speed limit and proxy.
        self._apply_policy(probe.final_url or self.config.url)
        if self.config.mirrors:
            self._mirrors = self._build_mirrors(probe)
        output = self._resolve_output(probe)
        self._output = output
        part = output.with_name(output.name + ".pdm.part")
        journal = JournalStore(output.with_name(output.name + ".pdm.json"))
        self._part = part
        self._journal = journal

        log.info(
            "probe: status=%s size=%s ranges=%s etag=%s",
            probe.status,
            probe.total_size,
            probe.ranges,
            probe.etag,
        )
        log.info("target: %s", redact_url(probe.final_url))

        if probe.total_size == 0:
            return self._finish_empty(probe, output, part, journal)

        if probe.ranges and probe.total_size is not None:
            return self._run_multi(probe, output, part, journal)
        return self._run_single(probe, output, part, journal)

    def cancel(self) -> None:
        """Stop the download promptly (safe to call from any thread)."""
        self.cancel_event.set()
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # output / file preparation
    # ------------------------------------------------------------------
    def _resolve_output(self, probe: ProbeResult) -> Path:
        name = sanitize_filename(probe.filename or "download")
        raw = self.config.output
        if raw is None:
            output = Path.cwd() / name
        else:
            raw_text = str(raw)
            candidate = Path(raw)
            if candidate.exists() and candidate.is_dir():
                output = candidate / name
            elif raw_text.endswith(("/", "\\")):
                candidate.mkdir(parents=True, exist_ok=True)
                output = candidate / name
            else:
                output = candidate

        if output.exists():
            journal_path = Path(str(output) + ".pdm.json")
            part_path = Path(str(output) + ".pdm.part")
            can_resume = (
                self.config.resume
                and not self.config.restart
                and journal_path.exists()
                and part_path.exists()
            )
            if not can_resume and not self.config.overwrite:
                output = unique_path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        return output

    def _part_and_journal(
        self, output: Path
    ) -> tuple[Path, JournalStore]:
        return (
            output.with_name(output.name + ".pdm.part"),
            JournalStore(output.with_name(output.name + ".pdm.json")),
        )

    def _finish_empty(
        self, probe: ProbeResult, output: Path, part: Path, journal: JournalStore
    ) -> DownloadResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"")
        part.unlink(missing_ok=True)
        journal.delete()
        digest = hashlib.sha256(b"").hexdigest()
        if self.config.sha256 and self.config.sha256.lower() != digest:
            raise ChecksumMismatch(self.config.sha256, digest)
        elapsed = max(1e-9, time.monotonic() - self._started_at)
        return DownloadResult(
            url=self.config.url,
            final_url=probe.final_url,
            path=output,
            total_size=0,
            downloaded=0,
            elapsed=elapsed,
            avg_speed=0.0,
            sha256=digest if self.config.sha256 else None,
            range_supported=probe.ranges,
            resumed=False,
            segments_created=0,
            peak_connections=0,
            retries=0,
        )

    # ------------------------------------------------------------------
    # multi-segment download
    # ------------------------------------------------------------------
    def _run_multi(
        self,
        probe: ProbeResult,
        output: Path,
        part: Path,
        journal: JournalStore,
    ) -> DownloadResult:
        total = probe.total_size
        assert total is not None
        segments, resumed = self._prepare_multi(probe, part, journal, total)
        self._resumed = resumed

        if all(seg.done for seg in segments):
            return self._finalize(
                probe, part, output, journal, total, resumed, len(segments), 0, 0, total
            )

        ceiling = self._effective_connections()
        starting = ceiling
        if self.config.adaptive:
            starting = max(1, min(ceiling, self.config.adaptive_start))
        state = DownloadState(
            segments=segments,
            total_size=total,
            max_connections=starting,
            min_split_size=self.config.min_split_size,
            dynamic=self.config.dynamic_segmentation,
            cancel_event=self.cancel_event,
            max_connections_limit=ceiling,
        )
        self._write_journal_multi(state, probe, output, part, journal)

        reporter = ProgressReporter(total, state, self.config, self.callback)
        reporter.start()
        saver_stop = threading.Event()
        saver = threading.Thread(
            target=self._saver_loop,
            args=(state, probe, output, part, journal, saver_stop),
            name="pdm-journal",
            daemon=True,
        )
        saver.start()

        adaptive_stop = threading.Event()
        adaptive: threading.Thread | None = None
        if self.config.adaptive:
            adaptive = threading.Thread(
                target=self._adaptive_loop,
                args=(state, adaptive_stop),
                name="pdm-aimd",
                daemon=True,
            )
            adaptive.start()

        pending_count = sum(1 for seg in segments if not seg.done)
        max_workers = self._effective_connections()
        worker_count = max_workers if resumed else pending_count
        worker_count = max(1, min(max_workers, worker_count))

        # With HTTP/2 one shared client multiplexes every segment over as few
        # TCP/TLS connections as the server allows (httpcore opens more only if
        # the protocol turns out to be HTTP/1.1, where a pool is equivalent to
        # one client per worker).  HTTP/3 needs the same treatment: one QUIC
        # connection carries every segment stream.  Without either flag the
        # per-worker design stays.
        shared_client: httpx.Client | None = None
        if self.config.http2 or self.config.http3:
            shared_client = make_client(
                self._request_config,
                cookies=probe.cookies,
                max_connections=max(1, worker_count),
            )
            self._register_client(shared_client)

        error: BaseException | None = None
        pool = ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="pdm"
        )
        try:
            futures = [
                pool.submit(self._worker, state, part, probe, shared_client)
                for _ in range(worker_count)
            ]
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    state.fail(exc)

            if state.error is not None:
                raise state.error
            if self.cancel_event.is_set():
                raise Cancelled("download cancelled")
            if not state.all_done():
                raise IncompleteTransfer("not all byte ranges completed")
        except BaseException as exc:
            error = exc
            if not isinstance(exc, Cancelled):
                state.fail(exc)
            # Close live sockets before shutdown() waits for the workers.
            self.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            adaptive_stop.set()
            if adaptive is not None:
                adaptive.join(timeout=2.0)
            if shared_client is not None:
                self._unregister_client(shared_client)
                try:
                    shared_client.close()
                except Exception:
                    pass
            saver_stop.set()
            saver.join(timeout=3.0)
            self._write_journal_multi(state, probe, output, part, journal)
            if isinstance(error, Cancelled) or (
                error is None and self.cancel_event.is_set()
            ):
                final_state = "cancelled"
            elif error is not None:
                final_state = "failed"
            else:
                final_state = "done"
            self._last_snapshot = reporter.stop(final_state)

        downloaded, _, created, retries = state.counters()
        result = self._finalize(
            probe,
            part,
            output,
            journal,
            total,
            resumed,
            created,
            state.peak_active,
            retries,
            downloaded,
        )
        result.final_connections = state.max_connections
        return result

    def _prepare_multi(
        self,
        probe: ProbeResult,
        part: Path,
        journal: JournalStore,
        total: int,
    ) -> tuple[list[Segment], bool]:
        resumed = False
        segments: list[Segment] | None = None

        if self.config.resume and not self.config.restart:
            data = journal.load()
            if data and data.get("mode") == "multi" and self._journal_matches(data, probe):
                segments = self._segments_from_journal(data, total)
                if segments is not None:
                    if not part.exists():
                        segments = None
                    else:
                        self._ensure_part_size(part, total)
                        part_size = part.stat().st_size
                        if any(seg.pos > part_size for seg in segments):
                            log.warning(
                                "journal cursor exceeds the part file; restarting"
                            )
                            segments = None
                        else:
                            resumed = any(seg.pos > seg.start for seg in segments)
                            if resumed:
                                log.info(
                                    "resuming %d segment(s), %d bytes already present",
                                    len(segments),
                                    sum(seg.pos - seg.start for seg in segments),
                                )

        if segments is None:
            if journal.path.exists():
                log.info("discarding partial state for a fresh download")
            journal.delete()
            segments = self._new_segments(total)
            self._reset_part(part, total)
            resumed = False
        else:
            self._ensure_part_size(part, total)
        if self._mirrors is not None:
            # A journal written with a different mirror list may hold stale
            # source indices; fold them back into range.
            self._mirrors.remap(segments)
        return segments, resumed

    def _journal_matches(self, data: dict, probe: ProbeResult) -> bool:
        if data.get("total_size") != probe.total_size:
            log.warning(
                "journal total size %r != server %r; restarting",
                data.get("total_size"),
                probe.total_size,
            )
            return False
        journal_validator = data.get("validator")
        current_validator = probe.validator()
        if (
            journal_validator
            and current_validator
            and journal_validator != current_validator
        ):
            log.warning("remote content changed (validator); restarting")
            return False
        return True

    def _segments_from_journal(
        self, data: dict, total: int
    ) -> list[Segment] | None:
        raw = data.get("segments")
        if not isinstance(raw, list) or not raw:
            return None
        try:
            segments = [Segment.from_dict(item) for item in raw]
        except (KeyError, TypeError, ValueError):
            return None
        segments.sort(key=lambda seg: seg.start)
        cursor = 0
        for seg in segments:
            if seg.start != cursor or seg.end <= seg.start or seg.end > total:
                return None
            if not (seg.start <= seg.pos <= seg.end):
                return None
            cursor = seg.end
        if cursor != total:
            return None
        return segments

    def _new_segments(self, total: int) -> list[Segment]:
        min_size = max(1, self.config.min_segment_size)
        count = min(self._effective_connections(), max(1, total // min_size))
        count = max(1, min(count, total))
        base, remainder = divmod(total, count)
        segments: list[Segment] = []
        start = 0
        for index in range(count):
            size = base + (1 if index < remainder else 0)
            end = start + size
            source = (
                self._mirrors.source_for(index) if self._mirrors is not None else 0
            )
            segments.append(
                Segment(index=index, start=start, end=end, source=source)
            )
            start = end
        return segments

    def _reset_part(self, part: Path, total: int) -> None:
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            with open(part, "wb") as handle:
                handle.truncate(total)
        except OSError as exc:
            raise DiskError(f"cannot create {part}: {exc}") from exc

    def _ensure_part_size(self, part: Path, total: int) -> None:
        try:
            if not part.exists():
                self._reset_part(part, total)
                return
            size = part.stat().st_size
            if size != total:
                with open(part, "r+b") as handle:
                    handle.truncate(total)
        except OSError as exc:
            raise DiskError(f"cannot prepare {part}: {exc}") from exc

    # ------------------------------------------------------------------
    # worker / segment streaming
    # ------------------------------------------------------------------
    def _worker(
        self,
        state: DownloadState,
        part: Path,
        probe: ProbeResult,
        shared_client: httpx.Client | None = None,
    ) -> None:
        fd: int | None = None
        # One client per source site, created lazily: mirrors may live on
        # different hosts with their own proxy/Cookie/auth rule.  Without
        # mirrors this is exactly the old "one client per worker".
        clients: dict[tuple[str, str], httpx.Client] = {}
        clients_lock = threading.Lock()

        def client_for(url: str, source_probe: ProbeResult) -> httpx.Client:
            if shared_client is not None:
                return shared_client
            parts = urlsplit(url)
            key = (parts.scheme, parts.netloc)
            with clients_lock:
                client = clients.get(key)
                if client is None:
                    client = make_client(
                        self._request_config_for(url),
                        cookies=source_probe.cookies,
                        max_connections=1,
                    )
                    self._register_client(client)
                    clients[key] = client
                return client

        try:
            fd = os.open(str(part), os.O_RDWR | getattr(os, "O_BINARY", 0))

            while not self.cancel_event.is_set():
                segment = state.next_segment()
                if segment is None:
                    return
                try:
                    self._download_segment(segment, fd, client_for, state, probe)
                    state.segment_finished(segment)
                except Cancelled:
                    state.release(segment)
                    return
                except BaseException as exc:
                    state.release(segment)
                    if self.cancel_event.is_set():
                        return
                    state.fail(exc)
                    return
        except BaseException as exc:
            if not self.cancel_event.is_set():
                state.fail(exc)
        finally:
            # A shared HTTP/2 client is closed by _run_multi, not here.
            if shared_client is None:
                with clients_lock:
                    owned = list(clients.values())
                    clients.clear()
                for client in owned:
                    self._unregister_client(client)
                    try:
                        client.close()
                    except Exception:
                        pass
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _download_segment(
        self,
        segment: Segment,
        fd: int,
        client_for: Callable[[str, ProbeResult], httpx.Client],
        state: DownloadState,
        probe: ProbeResult,
    ) -> None:
        attempt = 0
        while True:
            if self.cancel_event.is_set():
                raise Cancelled("download cancelled")
            with segment.lock:
                if segment.pos >= segment.end:
                    return
                before = segment.pos
            try:
                # Per-host connection budget is shared by every download in
                # the process; a slot is held only while a request is live.
                source_url, source_probe = self._segment_source(segment, probe)
                if not self._governor.acquire(source_url, self.cancel_event):
                    raise Cancelled("download cancelled")
                try:
                    self._stream_segment(
                        segment, fd, client_for(source_url, source_probe),
                        state, source_probe,
                    )
                finally:
                    self._governor.release(source_url)
                with segment.lock:
                    finished = segment.pos >= segment.end
                if not finished:
                    raise IncompleteTransfer(
                        f"segment {segment.index} stopped at {segment.pos} "
                        f"(target {segment.end})"
                    )
                return
            except RetryableError as exc:
                attempt = self._retry_or_raise(
                    segment, state, attempt, before, exc, exc.retry_after
                )
            except httpx.TimeoutException as exc:
                attempt = self._retry_or_raise(
                    segment, state, attempt, before, exc, None
                )
            except httpx.TransportError as exc:
                if self.cancel_event.is_set():
                    raise Cancelled("download cancelled") from exc
                if self._is_certificate_error(exc):
                    raise FatalError(f"TLS certificate error: {exc}") from exc
                attempt = self._retry_or_raise(
                    segment, state, attempt, before, exc, None
                )
            except RangeNotSupported as exc:
                # A mirror that suddenly ignores Range is dropped; the same
                # bytes are fetched from another live mirror.
                if self._mirrors is not None and self._mirrors.failover(segment):
                    log.warning(
                        "segment %d: mirror lost range support, switching (%s)",
                        segment.index,
                        exc,
                    )
                    continue
                raise
            except HTTPStatusError as exc:
                if (
                    exc.status in (403, 404, 410)
                    and self._mirrors is not None
                    and self._mirrors.failover(segment)
                ):
                    log.warning(
                        "segment %d: mirror returned %s, switching",
                        segment.index,
                        exc.status,
                    )
                    continue
                raise

    def _stream_segment(
        self,
        segment: Segment,
        fd: int,
        client: httpx.Client,
        state: DownloadState,
        probe: ProbeResult,
    ) -> None:
        with segment.lock:
            pos = segment.pos
            end = segment.end
        if pos >= end:
            return

        headers = {"Range": f"bytes={pos}-{end - 1}"}
        validator = probe.validator()
        if validator:
            headers["If-Range"] = validator

        url = probe.final_url or self.config.url
        with client.stream("GET", url, headers=headers) as response:
            if self.cancel_event.is_set():
                raise Cancelled("download cancelled")

            status = response.status_code
            if status == 206:
                content_range = parse_content_range(
                    response.headers.get("content-range")
                )
                if content_range is None:
                    raise ProtocolError(
                        "206 response without a Content-Range header"
                    )
                if content_range.start != pos:
                    raise RetryableError(
                        f"server returned range starting at {content_range.start}, "
                        f"asked for {pos}"
                    )
                if (
                    probe.total_size is not None
                    and content_range.total is not None
                    and content_range.total != probe.total_size
                ):
                    raise ContentChanged(
                        f"file size changed from {probe.total_size} to "
                        f"{content_range.total}"
                    )
            elif status == 200:
                if pos != 0 or end < (probe.total_size or end):
                    raise RangeNotSupported(
                        "server ignored the Range request for "
                        f"bytes {pos}-{end - 1}. Content may have changed, or the "
                        "server lacks range support. Retry with --connections 1 "
                        "--restart."
                    )
                # A single segment covering the whole file is fine.
            elif status == 416:
                if pos >= end:
                    return
                raise FatalError(
                    f"HTTP 416 range not satisfiable for bytes {pos}-{end - 1}"
                )
            elif status in _RETRYABLE_STATUS:
                if status in (429, 503):
                    state.note_pressure(status)  # AIMD congestion signal
                raise RetryableError(
                    f"HTTP {status} {response.reason_phrase}",
                    parse_retry_after(response.headers.get("retry-after")),
                )
            elif 400 <= status < 500:
                raise HTTPStatusError(
                    status, response.reason_phrase, str(response.url)
                )
            elif status >= 500:
                raise RetryableError(
                    f"HTTP {status} {response.reason_phrase}",
                    parse_retry_after(response.headers.get("retry-after")),
                )
            else:
                raise ProtocolError(
                    f"unexpected HTTP {status} {response.reason_phrase}"
                )

            self._reject_encoded_body(response)

            written = 0
            for chunk in response.iter_raw():
                if self.cancel_event.is_set():
                    raise Cancelled("download cancelled")
                if not chunk:
                    continue
                with segment.lock:
                    live_end = segment.end
                if pos >= live_end:
                    break
                limit = live_end - pos
                data = chunk if len(chunk) <= limit else chunk[:limit]
                if not data:
                    break
                self._write_all(fd, pos, data)
                pos += len(data)
                written += len(data)
                with segment.lock:
                    if pos > segment.pos:
                        segment.pos = pos
                state.add_bytes(len(data))
                self._governor.consume(len(data))
                self._limiter.consume(len(data))

            with segment.lock:
                live_end = segment.end
            if pos < live_end:
                raise IncompleteTransfer(
                    f"connection closed after {written} bytes at offset {pos}; "
                    f"expected {live_end}"
                )

    def _retry_or_raise(
        self,
        segment: Segment,
        state: DownloadState,
        attempt: int,
        before: int,
        exc: BaseException,
        retry_after: float | None,
    ) -> int:
        if self.cancel_event.is_set():
            raise Cancelled("download cancelled") from exc
        if segment.pos > before:
            attempt = 0  # progress was made; reset the failure streak
        attempt += 1
        if attempt > self.config.max_retries:
            raise FatalError(
                f"segment {segment.index} failed after {attempt - 1} retries: {exc}"
            ) from exc
        state.add_retry()
        delay = (
            retry_after
            if retry_after is not None
            else self._backoff(attempt)
        )
        if retry_after is not None and delay <= 0:
            # A server saying "retry after 0 seconds" must not cause a hot loop.
            delay = 0.05
        log.warning(
            "segment %d at %d: %s; retry %d/%d in %.2fs",
            segment.index,
            segment.pos,
            exc,
            attempt,
            self.config.max_retries,
            delay,
        )
        self._sleep(delay)
        return attempt

    def _backoff(self, attempt: int) -> float:
        base = self.config.retry_backoff
        if base <= 0:
            return 0.0
        delay = min(30.0, base * (2 ** max(0, attempt - 1)))
        return delay * (0.75 + random.random() * 0.5)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self.cancel_event.is_set():
                raise Cancelled("download cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.cancel_event.wait(min(remaining, 0.1))

    @staticmethod
    def _is_certificate_error(exc: BaseException) -> bool:
        cause: BaseException | None = exc
        for _ in range(10):
            if cause is None:
                return False
            if isinstance(cause, ssl.SSLCertVerificationError):
                return True
            cause = cause.__cause__ or cause.__context__
        return False

    @staticmethod
    def _reject_encoded_body(response: httpx.Response) -> None:
        encoding = (response.headers.get("content-encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            raise FatalError(
                f"server sent Content-Encoding: {encoding}; byte offsets would "
                "describe the encoded stream, not the file. Use a server that "
                "honours Accept-Encoding: identity."
            )

    @staticmethod
    def _write_all(fd: int, offset: int, data: bytes) -> None:
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        except OSError as exc:
            raise DiskError(f"write failed at offset {offset}: {exc}") from exc

    # ------------------------------------------------------------------
    # single-connection fallback (no Range support, or unknown length)
    # ------------------------------------------------------------------
    def _run_single(
        self,
        probe: ProbeResult,
        output: Path,
        part: Path,
        journal: JournalStore,
    ) -> DownloadResult:
        total = probe.total_size
        resume_pos = 0
        data = None
        if self.config.resume and not self.config.restart:
            data = journal.load()
        if (
            data
            and data.get("mode") == "single"
            and data.get("total_size") == total
            and total is not None
            and probe.ranges
            and part.exists()
        ):
            journal_validator = data.get("validator")
            current_validator = probe.validator()
            if (
                not journal_validator
                or not current_validator
                or journal_validator == current_validator
            ):
                resume_pos = min(int(data.get("pos", 0)), total)

        if total is not None and probe.ranges and resume_pos < total:
            log.info("resuming single-connection download at %d", resume_pos)
        elif resume_pos:
            resume_pos = 0

        # Prepare the target file.
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            if total is not None:
                if not part.exists():
                    with open(part, "wb") as handle:
                        handle.truncate(total)
                else:
                    current = part.stat().st_size
                    with open(part, "r+b") as handle:
                        if current < resume_pos:
                            resume_pos = current
                        handle.truncate(total)
            else:
                resume_pos = 0
                if part.exists():
                    part.unlink()
                part.touch()
        except OSError as exc:
            raise DiskError(f"cannot prepare {part}: {exc}") from exc

        progress = SimpleProgress()
        progress.downloaded = resume_pos
        reporter = ProgressReporter(total, progress, self.config, self.callback)
        reporter.start()

        retries = 0
        pos = resume_pos
        attempt = 0
        error: BaseException | None = None
        fd: int | None = None
        client: httpx.Client | None = None
        last_save = time.monotonic()
        try:
            fd = os.open(str(part), os.O_RDWR | getattr(os, "O_BINARY", 0))
            client = make_client(
                self._request_config, cookies=probe.cookies, max_connections=1
            )
            self._register_client(client)
            while True:
                if self.cancel_event.is_set():
                    raise Cancelled("download cancelled")
                before = pos
                try:
                    if not self._governor.acquire(
                        self._transfer_host(), self.cancel_event
                    ):
                        raise Cancelled("download cancelled")
                    try:
                        pos = self._stream_single(
                            pos, total, probe, fd, client, progress, journal
                        )
                    finally:
                        self._governor.release(self._transfer_host())
                    break
                except RetryableError as exc:
                    if self.cancel_event.is_set():
                        raise Cancelled("download cancelled") from exc
                    if pos > before:
                        attempt = 0
                    attempt += 1
                    if attempt > self.config.max_retries:
                        raise FatalError(
                            f"single-stream download failed after "
                            f"{attempt - 1} retries: {exc}"
                        ) from exc
                    retries += 1
                    delay = (
                        exc.retry_after
                        if exc.retry_after is not None
                        else self._backoff(attempt)
                    )
                    if exc.retry_after is not None and delay <= 0:
                        delay = 0.05
                    if probe.ranges and total is not None:
                        log.warning(
                            "%s; retry %d/%d from offset %d in %.2fs",
                            exc,
                            attempt,
                            self.config.max_retries,
                            pos,
                            delay,
                        )
                    else:
                        log.warning(
                            "%s; server has no resume support, restarting from 0 "
                            "in %.2fs",
                            exc,
                            delay,
                        )
                        pos = 0
                        progress.downloaded = 0
                        with open(part, "r+b") as handle:
                            handle.truncate(0 if total is None else total)
                    self._sleep(delay)
                except httpx.TimeoutException as exc:
                    if self.cancel_event.is_set():
                        raise Cancelled("download cancelled") from exc
                    if pos > before:
                        attempt = 0
                    attempt += 1
                    if attempt > self.config.max_retries:
                        raise FatalError(
                            f"single-stream download failed: {exc}"
                        ) from exc
                    retries += 1
                    if not (probe.ranges and total is not None):
                        pos = 0
                        progress.downloaded = 0
                        with open(part, "r+b") as handle:
                            handle.truncate(0 if total is None else total)
                    self._sleep(self._backoff(attempt))
                except httpx.TransportError as exc:
                    if self.cancel_event.is_set():
                        raise Cancelled("download cancelled") from exc
                    if self._is_certificate_error(exc):
                        raise FatalError(f"TLS certificate error: {exc}") from exc
                    if pos > before:
                        attempt = 0
                    attempt += 1
                    if attempt > self.config.max_retries:
                        raise FatalError(
                            f"single-stream download failed: {exc}"
                        ) from exc
                    retries += 1
                    if not (probe.ranges and total is not None):
                        pos = 0
                        progress.downloaded = 0
                        with open(part, "r+b") as handle:
                            handle.truncate(0 if total is None else total)
                    self._sleep(self._backoff(attempt))

                if (
                    total is not None
                    and probe.ranges
                    and time.monotonic() - last_save > _PROGRESS_JOURNAL_INTERVAL
                ):
                    self._write_journal_single(probe, output, part, journal, pos)
                    last_save = time.monotonic()
        except BaseException as exc:
            error = exc
            raise
        finally:
            if client is not None:
                self._unregister_client(client)
                try:
                    client.close()
                except Exception:
                    pass
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._write_journal_single(probe, output, part, journal, pos)
            if isinstance(error, Cancelled) or (
                error is None and self.cancel_event.is_set()
            ):
                final_state = "cancelled"
            elif error is not None:
                final_state = "failed"
            else:
                final_state = "done"
            self._last_snapshot = reporter.stop(final_state)

        final_total = total if total is not None else pos
        if total is not None and pos != total:
            raise IncompleteTransfer(
                f"downloaded {pos} of {total} bytes"
            )
        return self._finalize(
            probe,
            part,
            output,
            journal,
            final_total,
            resume_pos > 0,
            1,
            1,
            retries,
            pos,
        )

    def _stream_single(
        self,
        pos: int,
        total: int | None,
        probe: ProbeResult,
        fd: int,
        client: httpx.Client,
        progress: SimpleProgress,
        journal: JournalStore,
    ) -> int:
        headers: dict[str, str] = {}
        if pos > 0 and probe.ranges:
            headers["Range"] = f"bytes={pos}-"
            validator = probe.validator()
            if validator:
                headers["If-Range"] = validator

        url = probe.final_url or self.config.url
        with client.stream("GET", url, headers=headers) as response:
            if self.cancel_event.is_set():
                raise Cancelled("download cancelled")
            status = response.status_code

            if status == 206:
                content_range = parse_content_range(
                    response.headers.get("content-range")
                )
                if content_range is None:
                    raise ProtocolError(
                        "206 response without a Content-Range header"
                    )
                if content_range.start != pos:
                    raise RetryableError(
                        f"server resumed at {content_range.start}, expected {pos}"
                    )
                if (
                    total is not None
                    and content_range.total is not None
                    and content_range.total != total
                ):
                    raise ContentChanged(
                        f"file size changed from {total} to {content_range.total}"
                    )
            elif status == 200:
                if pos > 0:
                    # If-Range failed or the server does not honour ranges.
                    log.warning("server ignored resume; restarting from byte 0")
                    pos = 0
                    progress.downloaded = 0
                    if total is not None:
                        with open(self._part, "r+b") as handle:  # type: ignore[arg-type]
                            handle.truncate(total)
            elif status == 416:
                if total is not None and pos >= total:
                    return pos
                raise FatalError(f"HTTP 416 for offset {pos}")
            elif status in _RETRYABLE_STATUS:
                raise RetryableError(
                    f"HTTP {status} {response.reason_phrase}",
                    parse_retry_after(response.headers.get("retry-after")),
                )
            elif 400 <= status < 500:
                raise HTTPStatusError(
                    status, response.reason_phrase, str(response.url)
                )
            elif status >= 500:
                raise RetryableError(
                    f"HTTP {status} {response.reason_phrase}",
                    parse_retry_after(response.headers.get("retry-after")),
                )
            else:
                raise ProtocolError(
                    f"unexpected HTTP {status} {response.reason_phrase}"
                )

            self._reject_encoded_body(response)

            last_journal = time.monotonic()
            for chunk in response.iter_raw():
                if self.cancel_event.is_set():
                    raise Cancelled("download cancelled")
                if not chunk:
                    continue
                if total is not None:
                    if pos >= total:
                        break
                    remaining = total - pos
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                self._write_all(fd, pos, chunk)
                pos += len(chunk)
                progress.add(len(chunk))
                self._governor.consume(len(chunk))
                self._limiter.consume(len(chunk))
                if (
                    probe.ranges
                    and total is not None
                    and time.monotonic() - last_journal >= _PROGRESS_JOURNAL_INTERVAL
                    and self._output is not None
                    and self._part is not None
                ):
                    self._write_journal_single(
                        probe, self._output, self._part, journal, pos
                    )
                    last_journal = time.monotonic()

        if total is not None and pos < total:
            raise IncompleteTransfer(
                f"connection closed at {pos} of {total} bytes"
            )
        return pos

    # ------------------------------------------------------------------
    # journal
    # ------------------------------------------------------------------
    def _write_journal_multi(
        self,
        state: DownloadState,
        probe: ProbeResult,
        output: Path,
        part: Path,
        journal: JournalStore,
    ) -> None:
        journal.save(
            {
                "mode": "multi",
                "url": self.config.url,
                "final_url": probe.final_url,
                "total_size": probe.total_size,
                "etag": probe.etag,
                "last_modified": probe.last_modified,
                "validator": probe.validator(),
                "output": str(output),
                "part": part.name,
                "segments": state.snapshot(),
            }
        )

    def _write_journal_single(
        self,
        probe: ProbeResult,
        output: Path,
        part: Path,
        journal: JournalStore,
        pos: int,
    ) -> None:
        if not (probe.ranges and probe.total_size is not None):
            return
        journal.save(
            {
                "mode": "single",
                "url": self.config.url,
                "final_url": probe.final_url,
                "total_size": probe.total_size,
                "etag": probe.etag,
                "last_modified": probe.last_modified,
                "validator": probe.validator(),
                "output": str(output),
                "part": part.name,
                "pos": pos,
            }
        )

    def _adaptive_loop(self, state: DownloadState, stop: threading.Event) -> None:
        """Feed progress and pressure into the AIMD controller."""
        controller = AimdController(
            minimum=1,
            maximum=state.max_connections_limit,
            start=state.max_connections,
            interval=self.config.adaptive_interval,
        )
        last_bytes = state.downloaded
        last_pressure = state.pressure
        last_time = time.monotonic()
        while not stop.wait(0.2):
            now = time.monotonic()
            elapsed = now - last_time
            if elapsed <= 0:
                continue
            downloaded = state.downloaded - last_bytes
            pressure = state.pressure
            if pressure != last_pressure:
                value = controller.on_pressure(now)
            else:
                value = controller.observe(now, downloaded, elapsed)
            if value != state.max_connections:
                log.debug(
                    "AIMD: connections %d -> %d",
                    state.max_connections,
                    state.set_max_connections(value),
                )
            last_bytes = state.downloaded
            last_pressure = pressure
            last_time = now

    def _saver_loop(
        self,
        state: DownloadState,
        probe: ProbeResult,
        output: Path,
        part: Path,
        journal: JournalStore,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(_PROGRESS_JOURNAL_INTERVAL):
            self._write_journal_multi(state, probe, output, part, journal)

    # ------------------------------------------------------------------
    # completion
    # ------------------------------------------------------------------
    def _finalize(
        self,
        probe: ProbeResult,
        part: Path,
        output: Path,
        journal: JournalStore,
        total: int,
        resumed: bool,
        segments_created: int,
        peak_connections: int,
        retries: int,
        downloaded: int,
    ) -> DownloadResult:
        try:
            size = part.stat().st_size
        except OSError as exc:
            raise DiskError(f"cannot stat {part}: {exc}") from exc
        if size != total:
            raise IncompleteTransfer(
                f"part file is {size} bytes, expected {total}"
            )

        digest: str | None = None
        if self.config.sha256:
            digest = self._sha256(part)
            if digest.lower() != self.config.sha256.lower():
                raise ChecksumMismatch(self.config.sha256, digest)
            log.info("SHA-256 verified: %s", digest)

        try:
            if output.exists() and not self.config.overwrite:
                # _resolve_output normally prevents this; this guard protects
                # against a file created by another process mid-download.
                output = unique_path(output)
            os.replace(part, output)
        except OSError as exc:
            raise DiskError(f"cannot move {part} to {output}: {exc}") from exc
        journal.delete()

        elapsed = max(1e-9, time.monotonic() - self._started_at)
        return DownloadResult(
            url=self.config.url,
            final_url=probe.final_url,
            path=output,
            total_size=total,
            downloaded=downloaded,
            elapsed=elapsed,
            avg_speed=downloaded / elapsed,
            sha256=digest,
            range_supported=probe.ranges,
            resumed=resumed,
            segments_created=segments_created,
            peak_connections=peak_connections,
            retries=retries,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # client registry (for immediate cancellation)
    # ------------------------------------------------------------------
    def _register_client(self, client: httpx.Client) -> None:
        with self._clients_lock:
            self._clients.add(client)

    def _unregister_client(self, client: httpx.Client) -> None:
        with self._clients_lock:
            self._clients.discard(client)
