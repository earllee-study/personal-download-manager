"""Update checking and verified installer download.

This is a *user-initiated* updater: it never checks automatically, never
installs silently and never runs a downloaded file without an explicit action.
The manifest is a small JSON document:

    {
      "version": "0.2.0",
      "url": "https://example.com/pdm-setup-0.2.0.exe",
      "sha256": "…64 hex characters…",
      "size": 71303168,
      "notes": "…optional release notes…",
      "page_url": "https://example.com/downloads",
      "published_at": "2026-11-01"
    }

Security rules enforced here:
  * the manifest and the package must use HTTPS; plain HTTP is accepted only
    for loopback hosts so local tests work;
  * redirects are followed, but the *final* URL must satisfy the same rule;
  * credentials in URLs are rejected;
  * the package is streamed to a ``.part`` file, capped in size, and only
    published (atomic rename) after the declared SHA-256 matches;
  * launching requires an explicit caller action and a ``.exe`` file.

A manifest served over HTTPS and a matching SHA-256 give integrity, not
authenticity: whoever controls the manifest host can publish a hash for their
own file.  Code signing is still required to establish publisher identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from . import __version__
from .headers import sanitize_filename

DEFAULT_TIMEOUT = 30.0
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
MAX_NOTES_BYTES = 64 * 1024
_CHUNK = 256 * 1024
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+){0,3})(?:[-+].*)?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class UpdateError(Exception):
    """Base class for update failures shown to the user."""


class UpdateCheckError(UpdateError):
    """The manifest could not be fetched or is invalid."""


class UpdateDownloadError(UpdateError):
    """The update package could not be downloaded or verified."""


class UpdateCancelled(UpdateError):
    """The user cancelled the download."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    size: int | None = None
    notes: str = ""
    page_url: str | None = None
    published_at: str | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)


def parse_version(text: object) -> tuple[int, ...]:
    """Numeric dotted version; ``v1.2`` and ``1.2.0`` are accepted."""
    match = _VERSION_RE.match(str(text or "").strip())
    if not match:
        raise ValueError(f"invalid version: {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def version_is_newer(remote: object, current: object) -> bool:
    left = parse_version(remote)
    right = parse_version(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def _safe_url(url: object, what: str) -> str:
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise UpdateCheckError(f"{what} URL is invalid: {text!r}") from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise UpdateCheckError(f"{what} URL must be http(s): {text!r}")
    if parts.username is not None or parts.password is not None:
        raise UpdateCheckError(f"{what} URL must not contain credentials")
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise UpdateCheckError(
            f"{what} URL must use HTTPS (plain HTTP is allowed only for loopback)"
        )
    return text


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        trust_env=False,
        timeout=timeout,
        headers={
            "User-Agent": f"PDM/{__version__} updater",
            "Accept-Encoding": "identity",
        },
    )


def _read_manifest(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > MAX_MANIFEST_BYTES:
                raise UpdateCheckError("update manifest is too large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(_CHUNK):
        total += len(chunk)
        if total > MAX_MANIFEST_BYTES:
            raise UpdateCheckError("update manifest is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_manifest(data: bytes | str | dict, *, source: str = "update manifest") -> UpdateInfo:
    """Validate a manifest and return the update description."""
    if isinstance(data, dict):
        payload = data
    else:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if len(raw) > MAX_MANIFEST_BYTES:
            raise UpdateCheckError(f"{source} is too large")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateCheckError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateCheckError(f"{source} must be a JSON object")

    version = str(payload.get("version") or "").strip()
    try:
        parse_version(version)
    except ValueError as exc:
        raise UpdateCheckError(str(exc)) from exc

    url = _safe_url(payload.get("url"), "update package")
    if not urlsplit(url).path.lower().endswith(".exe"):
        raise UpdateCheckError("update package URL must point to a .exe installer")

    sha256 = str(payload.get("sha256") or "").strip().lower()
    if not _SHA256_RE.match(sha256):
        raise UpdateCheckError("update manifest needs a 64-character SHA-256")

    size = payload.get("size")
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise UpdateCheckError("update size must be an integer") from exc
        if not 0 < size <= MAX_PACKAGE_BYTES:
            raise UpdateCheckError("update size is out of range")

    notes = payload.get("notes") or ""
    if not isinstance(notes, str):
        raise UpdateCheckError("update notes must be a string")
    if len(notes.encode("utf-8")) > MAX_NOTES_BYTES:
        raise UpdateCheckError("update notes are too large")

    page_url = payload.get("page_url")
    if page_url:
        page_url = _safe_url(page_url, "release page")

    published = payload.get("published_at")
    if published is not None and not isinstance(published, str):
        raise UpdateCheckError("published_at must be a string")

    return UpdateInfo(
        version=version,
        url=url,
        sha256=sha256,
        size=size,
        notes=notes,
        page_url=page_url or None,
        published_at=published,
        raw=payload,
    )


def check_update(
    manifest_url: str,
    *,
    current_version: str = __version__,
    timeout: float = DEFAULT_TIMEOUT,
) -> UpdateInfo | None:
    """Return the newer release, or ``None`` when already up to date."""
    manifest_url = _safe_url(manifest_url, "update manifest")
    try:
        with _client(timeout) as client:
            with client.stream("GET", manifest_url) as response:
                response.raise_for_status()
                _safe_url(str(response.url), "update manifest (final)")
                data = _read_manifest(response)
    except UpdateError:
        raise
    except httpx.HTTPError as exc:
        raise UpdateCheckError(f"cannot fetch update manifest: {exc}") from exc
    info = parse_manifest(data)
    try:
        newer = version_is_newer(info.version, current_version)
    except ValueError as exc:
        raise UpdateCheckError(
            f"invalid current version: {current_version!r}"
        ) from exc
    return info if newer else None


def _package_name(info: UpdateInfo) -> str:
    parts = urlsplit(info.url)
    raw = unquote(Path(parts.path).name)
    name = sanitize_filename(raw) if raw else ""
    if not name or not name.lower().endswith(".exe"):
        name = f"pdm-setup-{sanitize_filename(info.version)}.exe"
    return name


def _matches(path: Path, info: UpdateInfo) -> bool:
    try:
        if not path.is_file():
            return False
        if info.size is not None and path.stat().st_size != info.size:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(block)
        return digest.hexdigest() == info.sha256
    except OSError:
        return False


def default_update_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home()
    return root / "PersonalDownloadManager" / "updates"


def download_update(
    info: UpdateInfo,
    directory: Path | str | None = None,
    *,
    progress=None,
    cancel_event=None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Path:
    """Download and verify the installer; return the published file path."""
    url = _safe_url(info.url, "update package")
    target_dir = Path(directory) if directory else default_update_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateDownloadError(f"cannot create {target_dir}: {exc}") from exc
    final = target_dir / _package_name(info)
    if _matches(final, info):
        return final
    part = final.with_name(final.name + ".part")
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass

    digest = hashlib.sha256()
    downloaded = 0
    try:
        with _client(timeout) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                _safe_url(str(response.url), "update package (final)")
                declared = None
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError:
                        declared = None
                total = info.size or declared
                if total is not None and total > MAX_PACKAGE_BYTES:
                    raise UpdateDownloadError("update package is too large")
                with open(part, "wb") as handle:
                    for chunk in response.iter_bytes(_CHUNK):
                        if cancel_event is not None and cancel_event.is_set():
                            raise UpdateCancelled("update download cancelled")
                        downloaded += len(chunk)
                        if downloaded > MAX_PACKAGE_BYTES:
                            raise UpdateDownloadError("update package is too large")
                        if info.size is not None and downloaded > info.size:
                            raise UpdateDownloadError(
                                "update package is larger than the manifest says"
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        if progress is not None:
                            progress(downloaded, total)
                    handle.flush()
                    os.fsync(handle.fileno())
        if info.size is not None and downloaded != info.size:
            raise UpdateDownloadError(
                f"update package size mismatch: got {downloaded}, expected {info.size}"
            )
        actual = digest.hexdigest()
        if actual != info.sha256:
            raise UpdateDownloadError(
                "update package SHA-256 mismatch "
                f"(got {actual}, expected {info.sha256})"
            )
        os.replace(part, final)
        return final
    except UpdateError:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except httpx.HTTPError as exc:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateDownloadError(f"cannot download update: {exc}") from exc
    except OSError as exc:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateDownloadError(f"cannot write update package: {exc}") from exc


def launch_installer(path: Path | str) -> subprocess.Popen:
    """Run a verified installer with its normal UI (no silent flags)."""
    installer = Path(path)
    if sys.platform != "win32":
        raise UpdateError("launching the installer is only supported on Windows")
    if not installer.is_file() or installer.stat().st_size <= 0:
        raise UpdateError(f"installer does not exist or is empty: {installer}")
    if installer.suffix.lower() != ".exe":
        raise UpdateError("installer must be a .exe file")
    try:
        return subprocess.Popen([str(installer)], shell=False)
    except OSError as exc:
        raise UpdateError(f"cannot start installer: {exc}") from exc
