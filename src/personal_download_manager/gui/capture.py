"""Capture files dropped by the browser native host.

The native host (see ``personal_download_manager.browser_host``) validates a URL and writes one
JSON file per download into ``%APPDATA%\\PersonalDownloadManager\\capture``.  The GUI polls
that directory; this module turns the files into DownloadTasks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..headers import filename_from_url, sanitize_filename
from ..paths import capture_dir

log = logging.getLogger(__name__)

#: MSE completion records written by the native host (see ``browser_host``).
MSE_MANIFEST_PREFIX = "mse-session-"
#: A capture can carry a handful of tracks; anything larger is malformed.
MAX_MSE_MANIFEST_FILES = 16


@dataclass
class Capture:
    url: str
    filename: str | None = None
    referer: str | None = None
    cookie: str | None = None
    user_agent: str | None = None
    hls: bool = False
    dash: bool = False
    added_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "Capture":
        url = str(data.get("url") or "").strip()
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise ValueError(f"invalid URL: {exc}") from exc
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("only http(s) URLs are accepted")

        def text(key: str) -> str | None:
            value = data.get(key)
            return str(value) if value else None

        try:
            added = float(data.get("added_at") or 0.0)
        except (TypeError, ValueError):
            added = 0.0
        return cls(
            url=url,
            filename=text("filename"),
            referer=text("referer"),
            cookie=text("cookie"),
            user_agent=text("user_agent"),
            hls=bool(data.get("hls")),
            dash=bool(data.get("dash")),
            added_at=added,
        )


def load_captures(directory: Path) -> list[tuple[Path, Capture]]:
    """Read and remove every valid capture file; malformed ones are dropped."""
    if not directory.is_dir():
        return []
    captures: list[tuple[Path, Capture]] = []
    for path in sorted(directory.glob("capture-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("capture must be a JSON object")
            captures.append((path, Capture.from_dict(data)))
        except (OSError, ValueError, TypeError):
            log.warning("ignoring malformed browser capture %s", path)
            path.unlink(missing_ok=True)
    return captures


def make_task(capture: Capture, download_dir: Path, settings, reserved: set[Path] | None = None):
    """Build a DownloadTask for a capture (imports Qt lazily)."""
    from .model import DownloadTask

    name = capture.filename or filename_from_url(capture.url) or "download"
    # Browsers may send a full relative path; keep only the last component
    # before the Windows filename sanitizer runs.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if capture.hls and name.lower().endswith(".m3u8"):
        name = name[:-5] + ".ts"
    if capture.dash and name.lower().endswith(".mpd"):
        name = name[:-4] + ".mp4"
    name = sanitize_filename(name)
    folder = settings.category_folder(name)
    layer = Path(download_dir)
    if folder:
        layer = layer.joinpath(*folder.split("/"))
    output = layer / name
    taken = reserved if reserved is not None else set()
    if output.exists() or output in taken:
        base = output
        counter = 1
        while output.exists() or output in taken:
            output = base.with_name(f"{base.stem} ({counter}){base.suffix}")
            counter += 1

    options: dict = {
        "connections": settings.default_connections,
        "speed_limit": settings.speed_limit,
        "category": settings.default_category,
    }
    if capture.hls:
        options["hls"] = True
        options["hls_quality"] = "best"
    if capture.dash:
        options["dash"] = True
        options["hls_quality"] = "best"
        options["dash_track"] = "video"
    if capture.referer:
        options["referer"] = capture.referer
    if capture.cookie:
        options["cookie"] = capture.cookie
    if capture.user_agent:
        options["user_agent"] = capture.user_agent
    return DownloadTask(url=capture.url, output=str(output), options=options)


# ----------------------------------------------------------------------
# experimental MSE capture (multi-track, written by the native host)
# ----------------------------------------------------------------------
def mse_root(directory: Path) -> Path:
    return Path(directory) / "mse"


def is_mse_media_path(path, directory: Path | None = None) -> bool:
    """True for a real file inside ``<capture>/mse`` (cleanup safety check)."""
    root = mse_root(directory if directory is not None else capture_dir()).resolve()
    try:
        candidate = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return _within(candidate, root) and candidate.is_file()


def _within(path: Path, root: Path) -> bool:
    """True when ``path`` is inside ``root`` (case-insensitive on Windows)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    if os.name == "nt":
        left = os.path.normcase(str(path))
        right = os.path.normcase(str(root))
        return left == right or left.startswith(right + os.sep)
    return False


def _mse_file(value, root: Path) -> dict | None:
    """Validate one manifest file entry; drop anything outside the spool.

    The manifest is local data, but a crafted/edited one must not make the GUI
    add a task (and later delete files with it) outside the capture folder.
    """
    if not isinstance(value, dict):
        return None
    raw = str(value.get("path") or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        path = candidate.resolve()
        stat = path.stat()
    except (OSError, ValueError):
        return None
    if not _within(path, root):
        log.warning("ignoring MSE file outside the capture folder: %s", path)
        return None
    if not path.is_file() or stat.st_size <= 0:
        return None
    digest = str(value.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = None
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in ("video", "audio", "unknown"):
        kind = "unknown"
    return {
        "path": str(path),
        "size": int(stat.st_size),
        "sha256": digest,
        "mime": str(value.get("mime") or "")[:200],
        "kind": kind,
        "track": str(value.get("track") or "")[:64],
    }


@dataclass
class MseCapture:
    """One finished MSE capture session from the native host."""

    session: str
    files: list[dict]
    merged: dict | None = None
    merge_error: str | None = None
    errors: list[str] | None = None
    source_url: str | None = None
    title: str | None = None
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict, directory: Path) -> "MseCapture":
        root = mse_root(directory).resolve()
        session = str(data.get("session") or "").strip()[:128]
        if not session:
            raise ValueError("MSE manifest has no session id")
        raw_files = data.get("files")
        files: list[dict] = []
        if isinstance(raw_files, list):
            for item in raw_files[:MAX_MSE_MANIFEST_FILES]:
                cleaned = _mse_file(item, root)
                if cleaned is not None:
                    files.append(cleaned)
        # De-duplicate by absolute path; the host should never repeat one, but
        # a hand-edited manifest must not make delete-on-remove ambiguous.
        unique: list[dict] = []
        seen: set[str] = set()
        for item in files:
            if item["path"] in seen:
                continue
            seen.add(item["path"])
            unique.append(item)
        files = unique
        merged = _mse_file(data.get("merged"), root) if data.get("merged") else None

        if not files and merged is None:
            raise ValueError("MSE manifest has no usable files")

        source_url = str(data.get("source_url") or "").strip()
        try:
            parts = urlsplit(source_url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                source_url = None
        except ValueError:
            source_url = None
        title = str(data.get("title") or "").strip() or None
        if title:
            title = title[:512]
        try:
            created = float(data.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        errors = data.get("errors")
        return cls(
            session=session,
            files=files,
            merged=merged,
            merge_error=str(data.get("merge_error") or "") or None,
            errors=[str(item) for item in errors] if isinstance(errors, list) else [],
            source_url=source_url,
            title=title,
            created_at=created,
        )

    def primary(self) -> dict:
        """Merged output if present, else the largest video, else audio/file."""
        if self.merged is not None:
            return self.merged
        if not self.files:
            raise ValueError("MSE capture has no files")
        videos = [item for item in self.files if item["kind"] == "video"]
        audios = [item for item in self.files if item["kind"] == "audio"]
        pool = videos or audios or self.files
        return max(pool, key=lambda item: item["size"])

    def all_paths(self) -> list[str]:
        paths: list[str] = []
        for item in ([self.merged] if self.merged else []) + self.files:
            if item and item["path"] not in paths:
                paths.append(item["path"])
        return paths


def load_mse_captures(directory: Path) -> list[tuple[Path, MseCapture]]:
    """Read and remove every valid MSE completion record."""
    root = mse_root(directory)
    if not root.is_dir():
        return []
    captures: list[tuple[Path, MseCapture]] = []
    for path in sorted(root.glob(f"{MSE_MANIFEST_PREFIX}*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("MSE manifest must be a JSON object")
            captures.append((path, MseCapture.from_dict(data, directory)))
        except (OSError, ValueError, TypeError):
            log.warning("ignoring malformed MSE capture %s", path)
            path.unlink(missing_ok=True)
    return captures


def make_mse_task(capture: MseCapture):
    """Build a completed DownloadTask for an imported MSE capture.

    Captured media stays in the capture folder (moving multi-gigabyte tracks on
    the GUI thread would freeze the window); the task points at the merged file
    when ffmpeg produced one, otherwise at the largest video/audio track.  All
    other tracks are kept in ``options["extra_paths"]`` so deleting the task can
    remove the whole capture.
    """
    from .model import STATUS_COMPLETED, DownloadTask

    primary = capture.primary()
    extra_paths = [
        path for path in capture.all_paths() if path != primary["path"]
    ]
    created = capture.created_at or time.time()
    options = {
        "capture": "mse",
        "mse_session": capture.session,
        "mse_tracks": list(capture.files),
        "mse_merged": capture.merged is not None,
        "mse_merge_error": capture.merge_error,
        "mse_primary_sha256": primary.get("sha256"),
        "source_url": capture.source_url,
        "title": capture.title,
        "extra_paths": extra_paths,
    }
    return DownloadTask(
        url=capture.source_url or f"mse://{capture.session}",
        output=primary["path"],
        status=STATUS_COMPLETED,
        total_size=primary["size"],
        downloaded=primary["size"],
        added_at=created,
        completed_at=created,
        options=options,
    )
