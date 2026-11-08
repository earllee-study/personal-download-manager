"""Native messaging host for the PDM browser extension.

Protocol (Chrome/Edge): each message is a 4-byte little-endian length followed
by UTF-8 JSON on stdin/stdout.  The extension can ask for:

* ``{"action": "ping"}`` -> version info;
* ``{"action": "download", "url": ..., "filename": ..., "referer": ...,
  "cookie": ..., "user_agent": ...}`` -> a capture file is dropped into
  ``%APPDATA%\\PersonalDownloadManager\\capture`` and the running GUI queues it;
* the experimental ``mse_start``/``mse_chunk``/``mse_end``/``mse_end_all``/
  ``mse_abort``/``mse_abort_all`` actions -> multi-track, unencrypted fMP4/WebM
  bytes are written under ``capture\\mse``.  ``mse_end_all`` may merge the
  largest video and audio tracks with a fixed ``ffmpeg -c copy`` command when
  ffmpeg is configured; it never decrypts DRM and never accepts arbitrary
  output paths from the extension.

The host never downloads anything itself and never invokes a shell; its only
optional child process is ffmpeg, with arguments built from files it created.
Browsers can only reach it if the user explicitly installed the native
messaging manifest.

Run with ``python -m personal_download_manager.browser_host`` (or the packaged
``pdm-host.exe``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from . import __version__
from .paths import app_data_dir, capture_dir

MAX_MESSAGE = 8 * 1024 * 1024  # Chrome's own limit is 1 MB per message
MAX_FILENAME = 512
MAX_COOKIE = 64 * 1024
MAX_HEADER = 8 * 1024

# Experimental MSE capture is bounded per track and per capture.  It never
# decrypts anything; multi-track, unencrypted fMP4/WebM is the supported case.
# The host is authoritative: the extension no longer hard-caps chunks, so long
# captures can continue until these limits (or the free-disk guard) are hit.
def _env_bytes(name: str, default: int) -> int:
    """Read a positive byte limit from the environment, else use ``default``."""
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / (1024**3):g} GiB"
    if value >= 1024**2:
        return f"{value / (1024**2):g} MiB"
    return f"{value} B"


DEFAULT_MAX_MSE_TRACK = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_MSE_TOTAL = 16 * 1024 * 1024 * 1024
DEFAULT_MIN_FREE_MSE_BYTES = 1 * 1024 * 1024 * 1024
MAX_MSE_CHUNK = 4 * 1024 * 1024
MAX_MSE_TRACK = _env_bytes("PDM_MSE_MAX_TRACK_BYTES", DEFAULT_MAX_MSE_TRACK)
MAX_MSE_TOTAL = _env_bytes("PDM_MSE_MAX_TOTAL_BYTES", DEFAULT_MAX_MSE_TOTAL)
MIN_FREE_MSE_BYTES = _env_bytes("PDM_MSE_MIN_FREE_BYTES", DEFAULT_MIN_FREE_MSE_BYTES)
MAX_MSE_SESSIONS = 4
FFMPEG_ENV = "PDM_DOWNLOADER_FFMPEG"


# ----------------------------------------------------------------------
# framing
# ----------------------------------------------------------------------
def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("truncated message header")
    (length,) = struct.unpack("<I", header)
    if length == 0:
        raise ValueError("empty message")
    if length > MAX_MESSAGE:
        raise ValueError(f"message too large ({length} bytes)")
    payload = stream.read(length)
    if len(payload) != length:
        raise ValueError("truncated message body")
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()


# ----------------------------------------------------------------------
# extension / manifest helpers (also used by the installer)
# ----------------------------------------------------------------------
def extension_id_from_key(key_b64: str) -> str:
    """Chrome extension ID derived from the manifest's base64 public key."""
    der = base64.b64decode(key_b64)
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def native_manifest(name: str, host_path: str, extension_ids: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Personal Download Manager native host",
        "path": str(Path(host_path).resolve()),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/" for ext_id in extension_ids],
    }


def firefox_native_manifest(
    name: str, host_path: str, extension_ids: list[str]
) -> dict[str, Any]:
    """Firefox registers the same stdio protocol but allows gecko IDs.

    Firefox looks for ``allowed_extensions`` (the ``gecko.id`` values from the
    extension manifest) rather than Chrome's ``allowed_origins``.
    """
    return {
        "name": name,
        "description": "Personal Download Manager native host",
        "path": str(Path(host_path).resolve()),
        "type": "stdio",
        "allowed_extensions": list(extension_ids),
    }


# ----------------------------------------------------------------------
# message handling
# ----------------------------------------------------------------------
def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = "".join(char for char in text if char.isprintable() or char in "\t ")
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def _is_drm_mime(mime: str) -> bool:
    text = (mime or "").lower()
    return any(
        token in text
        for token in (
            "encv",
            "enca",
            "widevine",
            "playready",
            "fairplay",
            "clearkey",
            "drm",
        )
    )


def _mse_filename(value: Any, kind: str, mime: str = "") -> str:
    raw = _text(value, MAX_FILENAME) or ""
    name = Path(raw.replace("\\", "/")).name
    name = "".join(
        char for char in name if char not in '<>:"/\\|?*' and ord(char) >= 32
    ).strip(" .")
    is_webm = "webm" in (mime or "").lower()
    if is_webm:
        allowed = (".webm", ".weba")
        default_ext = ".webm"
    elif kind == "audio":
        allowed = (".m4a", ".aac")
        default_ext = ".m4a"
    else:
        allowed = (".mp4", ".m4v")
        default_ext = ".mp4"
    if not name:
        name = f"pdm-mse{default_ext}"
    if not name.lower().endswith(allowed):
        stem = name.rsplit(".", 1)[0] if "." in name else name
        name = f"{stem}{default_ext}"
    return name[:MAX_FILENAME]


def _unique_mse_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _manifest_track(item: dict[str, Any]) -> dict[str, Any]:
    """Plain, JSON-safe metadata for one finished track (paths stay absolute)."""
    record = {
        "path": str(item.get("path") or ""),
        "size": int(item.get("size") or 0),
        "sha256": item.get("sha256"),
        "mime": str(item.get("mime") or ""),
        "kind": str(item.get("kind") or "unknown"),
        "track": str(item.get("track") or ""),
    }
    sources = item.get("sources")
    if isinstance(sources, list) and sources:
        record["sources"] = [str(path) for path in sources]
    return record


def _mse_free_bytes(root: Path) -> int | None:
    """Free bytes on the capture volume, or ``None`` when it cannot be read."""
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None


class MseSessions:
    """Bounded, in-memory tracking of experimental MSE capture uploads."""

    def __init__(self, spool: Path) -> None:
        self.root = Path(spool) / "mse"
        self.sessions: dict[str, dict[str, Any]] = {}

    def start(self, message: dict[str, Any]) -> dict[str, Any]:
        mime = _text(message.get("mime"), MAX_HEADER) or "video/mp4"
        kind = str(message.get("kind") or "").strip().lower()
        if not kind:
            kind = "audio" if mime.lower().startswith("audio/") else "video"
        if kind not in ("video", "audio", "unknown"):
            return {"ok": False, "error": "MSE kind must be video, audio, or unknown"}
        if not mime.lower().startswith(("video/", "audio/")):
            return {"ok": False, "error": "MSE mime must be video/* or audio/*"}
        if _is_drm_mime(mime):
            return {"ok": False, "error": "DRM/EME MSE capture is not supported"}
        if len(self.sessions) >= MAX_MSE_SESSIONS:
            return {"ok": False, "error": "too many active MSE sessions"}
        track = _text(message.get("track"), 64) or f"track-{len(self.sessions) + 1}"
        self.root.mkdir(parents=True, exist_ok=True)
        free = _mse_free_bytes(self.root)
        if free is not None and free < MIN_FREE_MSE_BYTES:
            return {
                "ok": False,
                "error": (
                    "MSE capture needs "
                    f"{_format_bytes(MIN_FREE_MSE_BYTES)} free disk space"
                ),
            }
        session_id = uuid.uuid4().hex
        part = self.root / f"mse-{session_id}.part"
        part.touch()
        self.sessions[session_id] = {
            "part": part,
            "filename": _mse_filename(message.get("filename"), kind, mime),
            "kind": kind,
            "mime": mime,
            "track": track,
            # Page metadata from the extension, used by the GUI queue entry.
            "source_url": _text(message.get("source_url"), 8192),
            "title": _text(message.get("title"), 512),
            "seq": 0,
            "total": 0,
        }
        return {
            "ok": True,
            "session": session_id,
            "path": str(part),
            "track": track,
            "kind": kind,
            "mime": mime,
        }

    def chunk(self, message: dict[str, Any]) -> dict[str, Any]:
        session_id = str(message.get("session") or "")
        session = self.sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "unknown MSE session"}
        raw = str(message.get("data") or "")
        try:
            payload = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"invalid MSE chunk: {exc}"}
        if not payload:
            return {"ok": False, "error": "empty MSE chunk"}
        if len(payload) > MAX_MSE_CHUNK:
            return {"ok": False, "error": "MSE chunk exceeds 4 MiB"}
        try:
            seq = int(message.get("seq"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "MSE chunk sequence is required"}
        if seq != session["seq"]:
            return {
                "ok": False,
                "error": f"unexpected MSE chunk sequence {seq}; expected {session['seq']}",
            }
        if session["total"] + len(payload) > MAX_MSE_TRACK:
            return {
                "ok": False,
                "error": f"MSE track exceeds {_format_bytes(MAX_MSE_TRACK)}",
            }
        total_all = sum(item["total"] for item in self.sessions.values())
        if total_all + len(payload) > MAX_MSE_TOTAL:
            return {
                "ok": False,
                "error": f"MSE capture exceeds {_format_bytes(MAX_MSE_TOTAL)}",
            }
        free = _mse_free_bytes(self.root)
        if free is not None and free - len(payload) < MIN_FREE_MSE_BYTES:
            return {
                "ok": False,
                "error": (
                    "MSE capture stopped: low disk space "
                    f"(needs {_format_bytes(MIN_FREE_MSE_BYTES)} free)"
                ),
            }
        with open(session["part"], "ab") as handle:
            handle.write(payload)
        session["seq"] += 1
        session["total"] += len(payload)
        return {
            "ok": True,
            "bytes": session["total"],
            "track": session["track"],
            "total": total_all + len(payload),
        }

    def end(
        self, message: dict[str, Any], *, manifest: bool = True
    ) -> dict[str, Any]:
        session_id = str(message.get("session") or "")
        session = self.sessions.pop(session_id, None)
        if session is None:
            return {"ok": False, "error": "unknown MSE session"}
        part: Path = session["part"]
        if session["total"] <= 0:
            part.unlink(missing_ok=True)
            return {"ok": False, "error": "no MSE data was captured"}
        final = _unique_mse_path(self.root, session["filename"])
        try:
            os.replace(part, final)
        except OSError as exc:
            return {"ok": False, "error": f"cannot finalize MSE capture: {exc}"}
        digest = _sha256_file(final)
        record = {
            "ok": True,
            "path": str(final),
            "size": final.stat().st_size,
            "sha256": digest,
            "mime": session["mime"],
            "kind": session["kind"],
            "track": session["track"],
        }
        if manifest:
            # A direct mse_end (not via end_all) is a finished single track;
            # publish the same durable record the GUI polls for.
            try:
                path = self._write_manifest(
                    [record],
                    None,
                    None,
                    [],
                    session.get("source_url"),
                    session.get("title"),
                )
            except OSError as exc:
                record["manifest"] = None
                record["manifest_error"] = f"cannot write capture manifest: {exc}"
            else:
                record["manifest"] = str(path)
                record["manifest_error"] = None
        return record

    def _write_manifest(
        self,
        files: list[dict[str, Any]],
        merged: dict[str, Any] | None,
        merge_error: str | None,
        errors: list[str],
        source_url: str | None,
        title: str | None,
    ) -> Path:
        """Publish one atomic completion record for the GUI queue."""
        self.root.mkdir(parents=True, exist_ok=True)
        session_id = uuid.uuid4().hex
        payload = {
            "kind": "mse",
            "version": 1,
            "session": session_id,
            "created_at": time.time(),
            "app_version": __version__,
            "source_url": source_url,
            "title": title,
            "files": [_manifest_track(item) for item in files],
            "merged": _manifest_track(merged) if merged else None,
            "merge_error": merge_error,
            "errors": list(errors),
            "track_count": len(files),
        }
        path = self.root / f"mse-session-{session_id}.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def abort(self, message: dict[str, Any]) -> dict[str, Any]:
        session_id = str(message.get("session") or "")
        session = self.sessions.pop(session_id, None)
        if session is None:
            return {"ok": True}
        Path(session["part"]).unlink(missing_ok=True)
        return {"ok": True}

    def end_all(self, message: dict[str, Any] | None = None) -> dict[str, Any]:
        """Finalize every active track, then try to merge video + audio."""
        session_ids = list(self.sessions)
        if not session_ids:
            return {"ok": False, "error": "no MSE data was captured"}
        source_url = None
        title = None
        for item in self.sessions.values():
            source_url = source_url or item.get("source_url")
            title = title or item.get("title")
        files: list[dict[str, Any]] = []
        errors: list[str] = []
        for session_id in session_ids:
            # One aggregate manifest per capture; individual tracks must not
            # each become a separate GUI queue entry.
            reply = self.end({"session": session_id}, manifest=False)
            if reply.get("ok"):
                files.append(reply)
            else:
                errors.append(str(reply.get("error") or "unknown"))
        if not files:
            return {
                "ok": False,
                "error": errors[0] if errors else "no MSE data was captured",
                "errors": errors,
            }
        merged = None
        merge_error = None
        if len(files) >= 2:
            attempt = _merge_mse_tracks(files)
            if attempt.get("ok"):
                merged = attempt
            else:
                merge_error = attempt.get("error")
        first = files[0]
        reply = {
            "ok": True,
            "files": files,
            "merged": merged,
            "merge_error": merge_error,
            "errors": errors,
            "track_count": len(files),
            "path": first["path"],
            "size": first["size"],
            "sha256": first["sha256"],
        }
        try:
            manifest = self._write_manifest(
                files, merged, merge_error, errors, source_url, title
            )
        except OSError as exc:
            reply["manifest"] = None
            reply["manifest_error"] = f"cannot write capture manifest: {exc}"
        else:
            reply["manifest"] = str(manifest)
            reply["manifest_error"] = None
        return reply

    def abort_all(self, message: dict[str, Any] | None = None) -> dict[str, Any]:
        session_ids = list(self.sessions)
        for session_id in session_ids:
            self.abort({"session": session_id})
        return {"ok": True, "aborted": len(session_ids)}


def _configured_ffmpeg() -> str | None:
    """Find ffmpeg for optional multi-track muxing.

    Order: ``PDM_DOWNLOADER_FFMPEG``, the GUI's ``config.json`` ``ffmpeg_path``,
    then ``ffmpeg`` on PATH.  A configured but missing path is not fatal here:
    the host keeps the individual tracks and reports ``merge_error``.
    """
    configured = (os.environ.get(FFMPEG_ENV) or "").strip()
    if not configured:
        try:
            config = json.loads(
                (app_data_dir() / "config.json").read_text(encoding="utf-8")
            )
            configured = str(config.get("ffmpeg_path") or "").strip()
        except (OSError, ValueError, TypeError):
            configured = ""
    if not configured:
        return shutil.which("ffmpeg")
    path = Path(os.path.expandvars(os.path.expanduser(configured)))
    if path.is_dir():
        for candidate in (
            path / "ffmpeg.exe",
            path / "ffmpeg",
            path / "bin" / "ffmpeg.exe",
            path / "bin" / "ffmpeg",
        ):
            if candidate.is_file():
                return str(candidate)
        return None
    if path.is_file():
        return str(path)
    return shutil.which(configured)


def _mse_mux_command(
    ffmpeg: str, video: Path, audio: Path, output: Path
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        str(output),
    ]


def _run_ffmpeg(command: list[str]) -> None:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            **kwargs,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run ffmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out after 120s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > 2000:
            detail = "..." + detail[-2000:]
        raise RuntimeError(
            f"ffmpeg failed with exit code {completed.returncode}: {detail}"
        )


def _merge_mse_tracks(files: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge the largest video and audio tracks with ``ffmpeg -c copy``."""
    ffmpeg = _configured_ffmpeg()
    if not ffmpeg:
        return {
            "ok": False,
            "error": "ffmpeg not found (set PDM_DOWNLOADER_FFMPEG or install ffmpeg)",
        }
    videos = [item for item in files if item.get("kind") == "video"]
    audios = [item for item in files if item.get("kind") == "audio"]
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if videos and audios:
        pairs.append(
            (
                max(videos, key=lambda item: int(item.get("size") or 0)),
                max(audios, key=lambda item: int(item.get("size") or 0)),
            )
        )
    elif len(files) >= 2 and any(
        item.get("kind") not in ("video", "audio") for item in files
    ):
        # Unknown kinds: try both orders and let ffmpeg's stream mapping decide.
        pairs.extend([(files[0], files[1]), (files[1], files[0])])
    if not pairs:
        return {"ok": False, "error": "need one video and one audio track"}

    last_error = ""
    for video, audio in pairs:
        video_path = Path(str(video.get("path") or ""))
        audio_path = Path(str(audio.get("path") or ""))
        if not video_path.is_file() or not audio_path.is_file():
            last_error = "source track is missing"
            continue
        is_webm = "webm" in (
            str(video.get("mime") or "") + str(audio.get("mime") or "")
        ).lower()
        extension = ".webm" if is_webm else ".mp4"
        output = _unique_mse_path(
            video_path.parent, f"{video_path.stem}.merged{extension}"
        )
        command = _mse_mux_command(ffmpeg, video_path, audio_path, output)
        try:
            _run_ffmpeg(command)
        except RuntimeError as exc:
            last_error = str(exc)
            output.unlink(missing_ok=True)
            continue
        if not output.is_file() or output.stat().st_size <= 0:
            last_error = "ffmpeg produced no output"
            output.unlink(missing_ok=True)
            continue
        return {
            "ok": True,
            "path": str(output),
            "size": output.stat().st_size,
            "sha256": _sha256_file(output),
            "mime": str(video.get("mime") or ""),
            "sources": [str(video_path), str(audio_path)],
            "command": command,
        }
    return {"ok": False, "error": last_error or "ffmpeg merge failed"}


def handle_message(
    message: dict[str, Any],
    spool: Path,
    now: float | None = None,
    mse_sessions: MseSessions | None = None,
) -> dict[str, Any]:
    action = str(message.get("action") or "").strip().lower()
    if action == "ping":
        return {"ok": True, "version": __version__}
    if action.startswith("mse_"):
        if mse_sessions is None:
            return {"ok": False, "error": "MSE session manager not configured"}
        if action == "mse_start":
            return mse_sessions.start(message)
        if action == "mse_chunk":
            return mse_sessions.chunk(message)
        if action == "mse_end":
            return mse_sessions.end(message)
        if action == "mse_end_all":
            return mse_sessions.end_all(message)
        if action == "mse_abort":
            return mse_sessions.abort(message)
        if action == "mse_abort_all":
            return mse_sessions.abort_all(message)
        return {"ok": False, "error": f"unknown MSE action {action!r}"}
    if action != "download":
        return {"ok": False, "error": f"unknown action {action!r}"}

    url = _text(message.get("url"), 8192) or ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return {"ok": False, "error": "invalid URL"}
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return {"ok": False, "error": "only http(s) URLs are accepted"}

    path = parts.path.lower()
    kind = str(message.get("kind") or "").strip().lower()
    is_dash = kind == "dash" or path.endswith(".mpd")
    is_hls = kind == "hls" or path.endswith(".m3u8")

    capture = {
        "url": url,
        "filename": _text(message.get("filename"), MAX_FILENAME),
        "referer": _text(message.get("referer"), MAX_HEADER),
        "cookie": _text(message.get("cookie"), MAX_COOKIE),
        "user_agent": _text(message.get("user_agent"), MAX_HEADER),
        "hls": is_hls,
        "dash": is_dash,
        "added_at": time.time() if now is None else now,
    }

    spool.mkdir(parents=True, exist_ok=True)
    path = spool / f"capture-{uuid.uuid4().hex}.json"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(capture, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return {"ok": False, "error": f"cannot queue download: {exc}"}
    return {"ok": True, "queued": True}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    spool = Path(os.environ.get("PDM_DOWNLOADER_CAPTURE_DIR") or capture_dir())
    for index, value in enumerate(args):
        if value == "--spool" and index + 1 < len(args):
            spool = Path(args[index + 1])

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    mse_sessions = MseSessions(spool)
    while True:
        try:
            message = read_message(stdin)
        except ValueError as exc:
            write_message(stdout, {"ok": False, "error": str(exc)})
            return 1
        if message is None:
            return 0
        try:
            reply = handle_message(message, spool, mse_sessions=mse_sessions)
        except Exception as exc:  # never die on one bad request
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        write_message(stdout, reply)


if __name__ == "__main__":
    raise SystemExit(main())
