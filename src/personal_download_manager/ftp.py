"""FTP and explicit FTPS downloads.

FTP has no range request, but many servers support ``REST offset`` before
``RETR``.  When the server advertises SIZE + REST, PDM uses several
control/data connections, each reading one byte range and writing directly at
its absolute offset into one preallocated ``.part`` (no merge pass).  Dynamic
splitting mirrors the HTTP engine.  Servers without REST/SIZE, tiny files and
``-n 1`` use the original single stream.

``ftps://`` uses explicit TLS (``AUTH TLS`` + ``PROT P``) on port 21; implicit
FTPS (port 990) is not supported.  Data connections are passive by default;
``--ftp-active`` (``DownloadConfig.ftp_active``) uses PORT/EPRT instead, which
makes the server connect back to a client-side listener.  This works for both
plain FTP and explicit FTPS, and for every segmented connection independently.
"""

from __future__ import annotations

import ftplib
import logging
import os
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .errors import (
    AuthRequired,
    Cancelled,
    DiskError,
    FatalError,
    PdmError,
    IncompleteTransfer,
    ProbeError,
    RetryableError,
)
from .journal import JournalStore
from .models import (
    DownloadConfig,
    DownloadState,
    ProbeResult,
    Segment,
)
from .policy import default_governor
from .progress import ProgressReporter
from .ratelimit import TokenBucket
from .remote import (
    CancelRelay,
    RemoteLocation,
    RemoteMeta,
    _retry_delay,
    _sleep,
    check_remote_change,
    display_url,
    finalize_download,
    journal_matches,
    parse_remote_url,
    reject_proxy,
    remote_name,
    resolve_targets,
    save_multi_journal,
    single_stream_download,
)

log = logging.getLogger(__name__)

CHUNK = 256 * 1024


class RestUnsupported(FatalError):
    """The server rejected REST after the metadata probe claimed support."""



def _ssl_context(config: DownloadConfig) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not config.verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _parse_mdtm(reply: str) -> str | None:
    # "213 20260101123045" (optionally with .fraction)
    parts = reply.split(None, 1)
    if len(parts) != 2:
        return None
    return parts[1].strip() or None


def _map_error(exc: BaseException, url: str) -> None:
    """Translate ftplib/socket failures into engine errors."""
    if isinstance(exc, ftplib.error_perm):
        text = str(exc)
        code = text[:3]
        if code in ("530", "532"):
            raise AuthRequired(f"FTP authentication failed for {url}: {text}") from exc
        raise FatalError(f"FTP server rejected the request for {url}: {text}") from exc
    if isinstance(exc, ftplib.error_temp):
        raise RetryableError(f"FTP temporary failure for {url}: {exc}") from exc
    if isinstance(exc, (socket.timeout, TimeoutError)):
        raise RetryableError(f"FTP connection timed out for {url}") from exc
    if isinstance(exc, (OSError, EOFError, ftplib.error_reply, ftplib.error_proto)):
        raise RetryableError(f"FTP connection failed for {url}: {exc}") from exc
    raise exc


class _FtpSession:
    def __init__(self, config: DownloadConfig, location: RemoteLocation) -> None:
        self.config = config
        self.location = location
        self.meta: RemoteMeta | None = None
        self.can_rest: bool | None = None

    # -- connection ------------------------------------------------------
    def _connect(self) -> ftplib.FTP:
        location = self.location
        try:
            if location.scheme == "ftps":
                ftp: ftplib.FTP = ftplib.FTP_TLS(
                    context=_ssl_context(self.config)
                )
            else:
                ftp = ftplib.FTP()
            ftp.connect(
                location.host,
                location.port,
                timeout=self.config.connect_timeout,
            )
            user = location.username or "anonymous"
            password = location.password
            if password is None:
                password = "anonymous@" if user == "anonymous" else ""
            ftp.login(user, password)
            # Passive by default; active mode makes the server connect to a
            # listener this client opens for each data transfer (PORT/EPRT).
            ftp.set_pasv(not self.config.ftp_active)
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()
            try:
                ftp.voidcmd("TYPE I")
            except ftplib.error_perm:
                # Binary is mandatory for byte offsets; a server refusing TYPE I
                # would make resume unsafe.
                raise FatalError(
                    "FTP server does not accept binary mode for "
                    f"{display_url(self.config.url)}"
                )
            return ftp
        except PdmError:
            raise
        except BaseException as exc:
            _map_error(exc, display_url(self.config.url))

    # -- metadata --------------------------------------------------------
    def metadata(self) -> RemoteMeta:
        ftp = self._connect()
        try:
            size = None
            try:
                size = ftp.size(self.location.path)
            except ftplib.error_perm:
                pass
            mtime = None
            try:
                mtime = _parse_mdtm(ftp.sendcmd(f"MDTM {self.location.path}"))
            except ftplib.all_errors:
                pass
            # REST capability decides whether a segmented download is safe.
            self.can_rest = None
            if size is not None and size > 0:
                probe_offset = 1 if size > 1 else 0
                try:
                    reply = ftp.sendcmd(f"REST {probe_offset}")
                    self.can_rest = str(reply).startswith("350")
                except ftplib.error_perm:
                    self.can_rest = False
                except ftplib.all_errors:
                    self.can_rest = None
            self.meta = RemoteMeta(
                filename=remote_name(self.config, self.location.path),
                total_size=size,
                mtime=mtime,
            )
            return self.meta
        except PdmError:
            raise
        except BaseException as exc:
            _map_error(exc, display_url(self.config.url))
        finally:
            try:
                ftp.quit()
            except BaseException:
                try:
                    ftp.close()
                except BaseException:
                    pass

    def _current_meta(self, ftp: ftplib.FTP) -> tuple[int | None, str | None]:
        size = None
        try:
            size = ftp.size(self.location.path)
        except ftplib.error_perm:
            pass
        mtime = None
        try:
            mtime = _parse_mdtm(ftp.sendcmd(f"MDTM {self.location.path}"))
        except ftplib.all_errors:
            pass
        return size, mtime

    # -- transfer --------------------------------------------------------
    def read_from(
        self,
        offset: int,
        total: int | None,
        sink: Callable[[bytes], None],
        cancel_event=None,
        relay: CancelRelay | None = None,
    ) -> None:
        ftp = self._connect()
        data: socket.socket | None = None
        state: dict[str, socket.socket | None] = {"data": None}

        def closer() -> None:
            live = state.get("data")
            if live is not None:
                try:
                    live.close()
                except OSError:
                    pass
            try:
                ftp.close()
            except BaseException:
                pass

        if relay is not None:
            relay.register(closer)
        try:
            size, mtime = self._current_meta(ftp)
            if self.meta is not None:
                check_remote_change(self.meta, size, mtime)
            if offset:
                try:
                    # REST succeeds with 350 (an intermediate reply), so this
                    # must use sendcmd() rather than voidcmd().
                    ftp.sendcmd(f"REST {offset}")
                except ftplib.error_perm as exc:
                    raise FatalError(
                        "FTP server does not support REST resume; "
                        "rerun with --restart"
                    ) from exc
            data = ftp.transfercmd(f"RETR {self.location.path}")
            state["data"] = data
            data.settimeout(1.0)
            last_data = time.monotonic()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled("download cancelled")
                try:
                    chunk = data.recv(CHUNK)
                except (socket.timeout, TimeoutError):
                    if time.monotonic() - last_data > self.config.read_timeout:
                        raise RetryableError(
                            "FTP data connection stalled for "
                            f"{display_url(self.config.url)}"
                        )
                    continue
                if not chunk:
                    break
                last_data = time.monotonic()
                sink(chunk)
            data.close()
            data = None
            state["data"] = None
            ftp.voidresp()
        except PdmError:
            raise
        except BaseException as exc:
            _map_error(exc, display_url(self.config.url))
        finally:
            if relay is not None:
                relay.unregister(closer)
            if data is not None:
                try:
                    data.close()
                except OSError:
                    pass
            try:
                ftp.close()
            except BaseException:
                pass


def _write_at(fd: int, offset: int, data: bytes) -> None:
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


def _stream_segment(
    session: _FtpSession,
    seg: Segment,
    fd: int,
    state: DownloadState,
    governor,
    bucket: TokenBucket,
    cancel_event,
    relay: CancelRelay,
) -> None:
    """REST at ``seg.pos``, RETR, write only this segment's live range."""
    ftp = session._connect()
    data: socket.socket | None = None
    holder: dict[str, socket.socket | None] = {"data": None}
    url = display_url(session.config.url)

    def closer() -> None:
        live = holder.get("data")
        if live is not None:
            try:
                live.close()
            except OSError:
                pass
        try:
            ftp.close()
        except BaseException:
            pass

    relay.register(closer)
    try:
        size, mtime = session._current_meta(ftp)
        if session.meta is not None:
            check_remote_change(session.meta, size, mtime)
        with seg.lock:
            pos = seg.pos
            live_end = seg.end
        if pos >= live_end:
            return
        if pos:
            try:
                # REST succeeds with 350; sendcmd accepts that intermediate reply.
                ftp.sendcmd(f"REST {pos}")
            except ftplib.error_perm as exc:
                raise RestUnsupported(
                    "FTP server rejected REST for a segmented download; "
                    "rerun with --connections 1"
                ) from exc
        data = ftp.transfercmd(f"RETR {session.location.path}")
        holder["data"] = data
        data.settimeout(1.0)
        last_data = time.monotonic()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise Cancelled("download cancelled")
            with seg.lock:
                live_end = seg.end
            if pos >= live_end:
                break
            try:
                chunk = data.recv(CHUNK)
            except (socket.timeout, TimeoutError):
                if time.monotonic() - last_data > session.config.read_timeout:
                    raise RetryableError(
                        f"FTP data connection stalled for {url}"
                    )
                continue
            if not chunk:
                break
            last_data = time.monotonic()
            take = min(len(chunk), live_end - pos)
            if take <= 0:
                break
            _write_at(fd, pos, chunk[:take])
            pos += take
            with seg.lock:
                if pos > seg.pos:
                    seg.pos = pos
            state.add_bytes(take)
            governor.consume(take)
            bucket.consume(take)
        if pos < live_end:
            raise IncompleteTransfer(
                f"FTP segment stopped at {pos} of {live_end} bytes"
            )
    except PdmError:
        raise
    except BaseException as exc:
        _map_error(exc, url)
    finally:
        relay.unregister(closer)
        if data is not None:
            try:
                data.close()
            except OSError:
                pass
        try:
            ftp.close()
        except BaseException:
            pass


def _new_segments(start: int, total: int, count: int, min_segment: int) -> list[Segment]:
    remaining = max(0, total - start)
    if remaining <= 0:
        return []
    count = max(1, min(count, remaining // max(1, min_segment)))
    base, extra = divmod(remaining, count)
    segments: list[Segment] = []
    offset = start
    for index in range(count):
        size = base + (1 if index < extra else 0)
        segments.append(
            Segment(index=index, start=offset, end=offset + size, pos=offset)
        )
        offset += size
    return segments


def _parse_segments(raw, total: int) -> list[Segment] | None:
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
    return segments if cursor == total else None


def _ensure_part(part: Path, total: int) -> None:
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if part.exists() else "wb"
        with open(part, mode) as handle:
            handle.truncate(total)
    except OSError as exc:
        raise DiskError(f"cannot prepare {part}: {exc}") from exc


def _load_segments(
    config: DownloadConfig,
    *,
    mode: str,
    meta: RemoteMeta,
    part: Path,
    journal: JournalStore,
    count: int,
    min_segment: int,
    total: int,
) -> tuple[list[Segment], int, bool]:
    """Return ``(segments, prefix_bytes, resumed)``.

    ``prefix_bytes`` is data before the first segment (an old single-stream
    journal); segment ``pos-start`` bytes are counted by DownloadState.
    """
    entry = None
    if config.resume and not config.restart and part.exists():
        entry = journal.load()
        if entry is not None and not journal_matches(entry, mode, config, meta):
            entry = None
    if entry is not None:
        segments = _parse_segments(entry.get("segments"), total)
        if segments is not None:
            try:
                part_size = part.stat().st_size
            except OSError:
                part_size = 0
            if part_size < total:
                # A multi journal always has a preallocated part; a shorter
                # file means the cursors cannot be trusted.  Restart safely.
                journal.delete()
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
                _ensure_part(part, total)
                return _new_segments(0, total, count, min_segment), 0, False
            _ensure_part(part, total)
            resumed = any(seg.pos > seg.start for seg in segments)
            return segments, 0, resumed
        try:
            pos = int(entry.get("pos", 0))
        except (TypeError, ValueError):
            pos = 0
        try:
            part_size = part.stat().st_size
        except OSError:
            part_size = 0
        pos = min(pos, part_size) if part_size else 0
        if pos >= total:
            _ensure_part(part, total)
            return [], pos, True
        if pos > 0:
            _ensure_part(part, total)
            start = min(pos, total)
            segments = _new_segments(start, total, count, min_segment)
            if segments:
                return segments, start, True
    journal.delete()
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass
    _ensure_part(part, total)
    return _new_segments(0, total, count, min_segment), 0, False


def _download_ftp_multi(
    config: DownloadConfig,
    location: RemoteLocation,
    session: _FtpSession,
    meta: RemoteMeta,
    max_connections: int,
    cancel_event,
    progress_callback,
):
    started = time.monotonic()
    total = meta.total_size
    assert total is not None and total > 0
    name = remote_name(config, location.path)
    output, part, journal = resolve_targets(config, name)
    count = max_connections
    min_segment = max(1, int(config.min_segment_size))
    segments, prefix, resumed = _load_segments(
        config,
        mode=location.scheme,
        meta=meta,
        part=part,
        journal=journal,
        count=count,
        min_segment=min_segment,
        total=total,
    )
    if not segments:
        # Everything was already present in an old journal.
        return finalize_download(
            config,
            started=started,
            output=output,
            part=part,
            journal=journal,
            total=total,
            downloaded=0,
            segments_created=0,
            peak_connections=0,
            retries=0,
            resumed=True,
            final_connections=1,
            range_supported=True,
        )

    governor = config.governor or default_governor()
    policy = governor.resolve(config.url)
    if policy.proxy:
        raise FatalError(
            "FTP through an HTTP proxy is not supported "
            f"(host rule for {display_url(config.url)})"
        )
    limits = [value for value in (config.speed_limit, policy.speed_limit) if value]
    bucket = TokenBucket(min(limits) if limits else None)

    state = DownloadState(
        segments=segments,
        total_size=total,
        max_connections=max(1, max_connections),
        min_split_size=min_segment,
        dynamic=config.dynamic_segmentation,
        cancel_event=cancel_event,
        max_connections_limit=max_connections,
    )
    # Bytes already on disk before the first segment (old single-stream resume)
    # are not represented by Segment.start; add them to progress once.
    state.downloaded += prefix
    initial_downloaded = state.downloaded

    reporter = ProgressReporter(total, state, config, progress_callback)
    reporter.start()
    saver_stop = threading.Event()

    def saver() -> None:
        while not saver_stop.wait(1.5):
            save_multi_journal(
                journal,
                mode=location.scheme,
                config=config,
                meta=meta,
                output=output,
                part=part,
                segments=state.snapshot(),
            )

    saver_thread = threading.Thread(
        target=saver, name="pdm-ftp-journal", daemon=True
    )
    saver_thread.start()

    def save_now() -> None:
        save_multi_journal(
            journal,
            mode=location.scheme,
            config=config,
            meta=meta,
            output=output,
            part=part,
            segments=state.snapshot(),
        )

    def worker() -> None:
        fd = None
        try:
            fd = os.open(str(part), os.O_RDWR | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            state.fail(DiskError(f"cannot open {part}: {exc}"))
            return
        try:
            while True:
                seg = state.next_segment()
                if seg is None:
                    return
                try:
                    _download_segment(
                        config,
                        session,
                        seg,
                        fd,
                        state,
                        governor,
                        bucket,
                        cancel_event,
                        save_now,
                    )
                    state.segment_finished(seg)
                except Cancelled:
                    state.release(seg)
                    return
                except BaseException as exc:
                    state.release(seg)
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    state.fail(exc)
                    return
        finally:
            if fd is not None:
                os.close(fd)

    error: BaseException | None = None
    final_state = "done"
    try:
        worker_count = max(1, max_connections)
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="pdm-ftp"
        ) as pool:
            futures = [pool.submit(worker) for _ in range(worker_count)]
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    error = exc
                    state.fail(exc)
        if error is not None:
            raise error
        if state.error is not None:
            raise state.error
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("download cancelled")
        if not state.all_done():
            raise IncompleteTransfer("not all FTP byte ranges completed")
    except RestUnsupported:
        final_state = "failed"
        raise
    except Cancelled:
        final_state = "cancelled"
        raise
    except BaseException:
        final_state = "failed"
        raise
    finally:
        saver_stop.set()
        saver_thread.join(timeout=2.0)
        if final_state != "done":
            save_multi_journal(
                journal,
                mode=location.scheme,
                config=config,
                meta=meta,
                output=output,
                part=part,
                segments=state.snapshot(),
            )
        reporter.stop(final_state)

    _, _, created, retries = state.counters()
    return finalize_download(
        config,
        started=started,
        output=output,
        part=part,
        journal=journal,
        total=total,
        downloaded=max(0, state.downloaded - initial_downloaded),
        segments_created=created,
        peak_connections=state.peak_active,
        retries=retries,
        resumed=resumed,
        final_connections=state.max_connections,
        range_supported=True,
    )


def _download_segment(
    config: DownloadConfig,
    session: _FtpSession,
    seg: Segment,
    fd: int,
    state: DownloadState,
    governor,
    bucket: TokenBucket,
    cancel_event,
    save_now: Callable[[], None],
) -> None:
    attempt = 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("download cancelled")
        with seg.lock:
            if seg.pos >= seg.end:
                return
            before = seg.pos
        acquired = governor.acquire(config.url, cancel_event)
        if not acquired:
            raise Cancelled("download cancelled")
        try:
            _stream_segment(
                session, seg, fd, state, governor, bucket, cancel_event, cancel_event
            )
            with seg.lock:
                if seg.pos < seg.end:
                    raise IncompleteTransfer(
                        f"FTP segment stopped at {seg.pos} of {seg.end} bytes"
                    )
            return
        except RetryableError as exc:
            state.add_retry()
            if seg.pos > before:
                attempt = 0
            attempt += 1
            if attempt > config.max_retries:
                raise FatalError(
                    f"FTP segment {seg.index} failed after "
                    f"{attempt - 1} retries: {exc}"
                ) from exc
            save_now()
            _sleep(_retry_delay(config, attempt, exc), cancel_event)
        finally:
            governor.release(config.url)


def _location(config: DownloadConfig) -> RemoteLocation:
    reject_proxy(config, "FTP")
    return parse_remote_url(config, default_port=21, schemes=("ftp", "ftps"))


def _probe_meta(session: _FtpSession) -> RemoteMeta:
    try:
        return session.metadata()
    except AuthRequired:
        raise
    except RetryableError as exc:
        raise ProbeError(str(exc), transient=True) from exc
    except FatalError as exc:
        raise ProbeError(str(exc)) from exc


def probe_ftp(config: DownloadConfig) -> ProbeResult:
    location = _location(config)
    session = _FtpSession(config, location)
    meta = _probe_meta(session)
    return ProbeResult(
        url=config.url,
        final_url=config.url,
        status=213,
        total_size=meta.total_size,
        ranges=bool(session.can_rest),
        last_modified=meta.mtime,
        filename=meta.filename,
        http_version="FTPS" if location.scheme == "ftps" else "FTP",
    )


def _effective_connections(config: DownloadConfig) -> int:
    governor = config.governor or default_governor()
    policy = governor.resolve(config.url)
    cap = policy.connections
    if cap is None:
        return max(1, int(config.max_connections))
    return max(1, min(int(config.max_connections), int(cap)))


def download_ftp(
    config: DownloadConfig,
    cancel_event=None,
    progress_callback=None,
):
    location = _location(config)
    session = _FtpSession(config, location)
    meta = _probe_meta(session)
    max_connections = _effective_connections(config)
    min_segment = max(1, int(config.min_segment_size))
    use_multi = (
        meta.total_size is not None
        and meta.total_size >= 2 * min_segment
        and max_connections > 1
        and session.can_rest is True
    )
    if use_multi:
        relay = CancelRelay(cancel_event).start()
        try:
            return _download_ftp_multi(
                config,
                location,
                session,
                meta,
                max_connections,
                relay,
                progress_callback,
            )
        finally:
            relay.stop()
    relay = CancelRelay(cancel_event).start()
    try:
        return single_stream_download(
            config,
            mode=location.scheme,
            meta=meta,
            read_from=lambda offset, total, sink: session.read_from(
                offset, total, sink, relay, relay
            ),
            cancel_event=relay,
            progress_callback=progress_callback,
            range_supported=bool(session.can_rest),
        )
    finally:
        relay.stop()
