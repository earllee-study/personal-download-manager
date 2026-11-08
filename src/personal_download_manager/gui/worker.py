"""QThread wrapper around one Downloader instance."""

from __future__ import annotations

import logging
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Signal

from ..dash import download_dash, is_dash_url
from ..downloader import Downloader
from ..errors import Cancelled, PdmError, is_retryable
from ..headers import parse_size
from ..hls import download_hls, is_hls_url
from ..models import DownloadConfig
from .model import DownloadTask
from .settings import AppSettings

log = logging.getLogger(__name__)


class DownloadWorker(QThread):
    progress = Signal(str, dict)      # task id, ProgressSnapshot dict
    succeeded = Signal(str, dict)     # task id, DownloadResult dict
    failed = Signal(str, str, bool)   # task id, message, worth retrying later?
    cancelled = Signal(str)           # task id

    def __init__(
        self,
        task: DownloadTask,
        settings: AppSettings,
        governor=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.task = task
        self.settings = settings
        self.governor = governor
        self._downloader: Downloader | None = None
        self._cancel_requested = False
        self._cancel_event = threading.Event()

    def run(self) -> None:  # noqa: D401
        if self._cancel_requested:
            self.cancelled.emit(self.task.id)
            return
        try:
            result = self._run()
            self.succeeded.emit(self.task.id, result.to_dict())
        except Cancelled:
            self.cancelled.emit(self.task.id)
        except PdmError as exc:
            self.failed.emit(self.task.id, str(exc), is_retryable(exc))
        except Exception as exc:  # keep the UI alive on unexpected bugs
            log.exception("unexpected download failure")
            # Treat a bug as permanent: an outer retry would just repeat it.
            self.failed.emit(self.task.id, f"{type(exc).__name__}: {exc}", False)

    def _run(self):
        from ..urlbatch import is_http_url

        config = self._build_config()
        streaming = is_http_url(self.task.url)
        if streaming and (
            self.task.options.get("dash") or is_dash_url(self.task.url)
        ):
            if self._cancel_requested:
                raise Cancelled("download cancelled")
            return download_dash(
                config,
                quality=self.task.options.get("hls_quality"),
                track_kind=self.task.options.get("dash_track") or "video",
                cancel_event=self._cancel_event,
                progress_callback=self._on_stream_progress,
            )
        if streaming and (
            self.task.options.get("hls") or is_hls_url(self.task.url)
        ):
            if self._cancel_requested:
                raise Cancelled("download cancelled")
            return download_hls(
                config,
                quality=self.task.options.get("hls_quality"),
                cancel_event=self._cancel_event,
                progress_callback=self._on_stream_progress,
            )
        self._downloader = Downloader(config, progress_callback=self._on_progress)
        if self._cancel_requested:
            self._downloader.cancel()
        return self._downloader.run()

    def cancel(self) -> None:
        self._cancel_requested = True
        self._cancel_event.set()
        if self._downloader is not None:
            self._downloader.cancel()

    def _on_progress(self, snapshot) -> None:
        # Called from the engine's progress thread; Qt queues this to the GUI.
        self.progress.emit(self.task.id, snapshot.to_dict())

    def _on_stream_progress(self, payload: dict) -> None:
        self.progress.emit(self.task.id, payload)

    def _build_config(self) -> DownloadConfig:
        opts = self.task.options
        scheme = urlsplit(self.task.url).scheme.lower()
        min_segment = _size(opts.get("min_segment"), 1024 * 1024)
        min_split = _size(opts.get("min_split"), 1024 * 1024)
        limit = opts["speed_limit"] if "speed_limit" in opts else self.settings.speed_limit
        auth = opts.get("auth")
        if auth and not isinstance(auth, (tuple, list)):
            auth = None
        return DownloadConfig(
            url=self.task.url,
            output=self.task.output,
            max_connections=int(opts.get("connections", self.settings.default_connections)),
            min_segment_size=min_segment,
            min_split_size=min_split,
            dynamic_segmentation=bool(opts.get("dynamic", True)),
            adaptive=bool(opts.get("adaptive", False)),
            mirrors=[str(item) for item in (opts.get("mirrors") or []) if item],
            max_retries=int(opts.get("retries", 5)),
            retry_backoff=float(opts.get("retry_backoff", 1.0)),
            speed_limit=limit,
            user_agent=opts.get("user_agent") or self.settings.user_agent,
            headers=dict(opts.get("headers") or {}),
            referer=opts.get("referer") or None,
            cookie=opts.get("cookie") or None,
            auth=tuple(auth) if auth else None,  # type: ignore[arg-type]
            auth_type=opts.get("auth_type", "basic"),
            # HTTP proxies (including the global default) do not apply to
            # FTP/SFTP; the remote driver rejects one explicitly.
            proxy=(
                opts.get("proxy") or self.settings.proxy
            )
            if scheme in ("http", "https")
            else None,
            verify=bool(opts.get("verify_tls", self.settings.verify_tls)),
            http2=bool(opts.get("http2", False)),
            http3=bool(opts.get("http3", False)),
            ftp_active=bool(opts.get("ftp_active", False))
            if scheme in ("ftp", "ftps")
            else False,
            sftp_key=opts.get("sftp_key") or None,
            sftp_key_pass=opts.get("sftp_key_pass") or None,
            sftp_known_hosts=opts.get("sftp_known_hosts") or None,
            sftp_auto_host_key=bool(opts.get("sftp_auto_host_key", False)),
            ffmpeg_path=opts.get("ffmpeg_path") or self.settings.ffmpeg_path or None,
            sha256=opts.get("sha256") or None,
            # A recurring task must replace last run's file at the same path,
            # not accumulate "file (1).zip" copies every day.
            overwrite=bool(opts.get("overwrite", False) or self.task.repeat),
            show_progress=False,
            json_progress=False,
            quiet=True,
            progress_interval=0.2,
            governor=self.governor,
        )


def _size(value, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = parse_size(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed and parsed > 0 else default
