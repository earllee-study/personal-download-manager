"""HLS (HTTP Live Streaming) VOD downloader.

Scope: VOD playlists -- master playlists with variants, media playlists with
``EXT-X-BYTERANGE``, ``EXT-X-MAP``, relative URIs, and the standard
``METHOD=AES-128`` encryption with a retrievable key URI.

AES-128 is decrypted locally (AES-CBC + PKCS#7).  SAMPLE-AES / DRM key formats
and live/event playlists without ``EXT-X-ENDLIST`` are refused: this project
does not implement DRM or key-system bypasses.  Refusing outright beats
silently writing a corrupt file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import padding as _padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .client import apply_policy, make_client
from .errors import Cancelled, ChecksumMismatch, FatalError
from .headers import unique_path
from .models import DownloadConfig, DownloadResult
from .policy import default_governor
from .ratelimit import TokenBucket
from .streaming import (
    StreamProgress as _Progress,
    check_cancel as _check_cancel,
    download_stream_segment as _download_segment,
    report_loop as _report_loop,
)

log = logging.getLogger(__name__)


def is_hls_url(url: str) -> bool:
    """Heuristic used by the GUI to route playlist URLs to the HLS engine."""
    try:
        return urlsplit(url).path.lower().endswith(".m3u8")
    except ValueError:
        return False


# ----------------------------------------------------------------------
# playlist model / parser
# ----------------------------------------------------------------------
@dataclass
class Variant:
    uri: str
    bandwidth: int | None = None
    resolution: str | None = None


@dataclass(frozen=True)
class HlsKey:
    method: str
    uri: str | None
    iv: bytes | None = None
    keyformat: str | None = None


@dataclass
class MediaSegment:
    uri: str
    duration: float = 0.0
    byterange: tuple[int, int] | None = None  # (length, offset)
    sequence: int = 0
    key: HlsKey | None = None


@dataclass
class MediaPlaylist:
    url: str
    segments: list[MediaSegment] = field(default_factory=list)
    init_uri: str | None = None
    init_byterange: tuple[int, int] | None = None
    init_key: HlsKey | None = None
    init_iv: bytes | None = None
    ended: bool = False
    encrypted: bool = False
    encryption_error: str | None = None


@dataclass
class Playlist:
    is_master: bool
    variants: list[Variant] = field(default_factory=list)
    media: MediaPlaylist | None = None


def _attribute(line: str, name: str) -> str | None:
    """Extract NAME=value from an HLS attribute list (quoted or bare)."""
    marker = f"{name}="
    index = line.find(marker)
    if index < 0:
        return None
    rest = line[index + len(marker) :]
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else None
    end = rest.find(",")
    return rest if end < 0 else rest[:end]


def _parse_byterange(value: str | None, previous_end: int) -> tuple[int, int] | None:
    if not value:
        return None
    value = value.strip().strip('"')
    if "@" in value:
        length_text, offset_text = value.split("@", 1)
        try:
            return int(length_text), int(offset_text)
        except ValueError:
            return None
    try:
        return int(value), previous_end
    except ValueError:
        return None


def _parse_iv(value: str | None) -> bytes | None:
    if not value:
        return None
    text = value.strip().strip('"')
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text or len(text) > 32:
        raise FatalError(f"invalid HLS IV {value!r}")
    try:
        return bytes.fromhex(text.zfill(32))
    except ValueError as exc:
        raise FatalError(f"invalid HLS IV {value!r}") from exc


def parse_playlist(text: str, base_url: str) -> Playlist:
    """Parse a master or media playlist; URIs are resolved against ``base_url``."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise FatalError("not an HLS playlist (missing #EXTM3U)")

    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        variants: list[Variant] = []
        pending: str | None = None
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF:"):
                pending = line
            elif not line.startswith("#") and pending is not None:
                bandwidth = _attribute(pending, "BANDWIDTH")
                variants.append(
                    Variant(
                        uri=urljoin(base_url, line),
                        bandwidth=int(bandwidth) if bandwidth and bandwidth.isdigit() else None,
                        resolution=_attribute(pending, "RESOLUTION"),
                    )
                )
                pending = None
        if not variants:
            raise FatalError("master playlist has no variants")
        return Playlist(is_master=True, variants=variants)

    media = MediaPlaylist(url=base_url)
    pending_duration = 0.0
    pending_byterange: tuple[int, int] | None = None
    previous_end = 0
    sequence = 0
    current_key: HlsKey | None = None
    for line in lines:
        if line.startswith("#EXT-X-ENDLIST"):
            media.ended = True
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                sequence = 0
        elif line.startswith("#EXT-X-KEY:"):
            method = (_attribute(line, "METHOD") or "NONE").strip().upper()
            keyformat = _attribute(line, "KEYFORMAT")
            if method == "NONE":
                current_key = None
            elif method == "AES-128":
                media.encrypted = True
                uri = _attribute(line, "URI")
                if keyformat and keyformat.strip().lower() != "identity":
                    media.encryption_error = (
                        f"unsupported HLS KEYFORMAT {keyformat!r} (DRM)"
                    )
                    current_key = None
                elif not uri:
                    media.encryption_error = "AES-128 EXT-X-KEY is missing URI"
                    current_key = None
                else:
                    current_key = HlsKey(
                        method="AES-128",
                        uri=urljoin(base_url, uri),
                        iv=_parse_iv(_attribute(line, "IV")),
                        keyformat=keyformat or "identity",
                    )
            else:
                media.encrypted = True
                media.encryption_error = (
                    f"unsupported HLS encryption method {method!r} "
                    "(SAMPLE-AES/DRM is not supported)"
                )
                current_key = None
        elif line.startswith("#EXT-X-MAP:"):
            uri = _attribute(line, "URI")
            if uri:
                media.init_uri = urljoin(base_url, uri)
                media.init_byterange = _parse_byterange(
                    _attribute(line, "BYTERANGE"), 0
                )
                # An encrypted init section must carry its own IV; without one
                # it is treated as clear.
                map_iv = _parse_iv(_attribute(line, "IV"))
                media.init_iv = map_iv
                media.init_key = current_key if map_iv is not None else None
        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending_duration = 0.0
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_byterange = _parse_byterange(line.split(":", 1)[1], previous_end)
        elif line.startswith("#"):
            continue
        else:
            media.segments.append(
                MediaSegment(
                    uri=urljoin(base_url, line),
                    duration=pending_duration,
                    byterange=pending_byterange,
                    sequence=sequence + len(media.segments),
                    key=current_key,
                )
            )
            if pending_byterange is not None:
                previous_end = pending_byterange[1] + pending_byterange[0]
            pending_duration = 0.0
            pending_byterange = None

    if not media.segments:
        raise FatalError("media playlist has no segments")
    return Playlist(is_master=False, media=media)


def choose_variant(variants: list[Variant], quality: str | None) -> Variant:
    """``quality`` is 'best', 'worst', a 0-based index, or a string to match."""
    if not variants:
        raise FatalError("no variants to choose from")
    if quality is None or quality in ("best", "auto"):
        return max(variants, key=lambda v: v.bandwidth or 0)
    if quality == "worst":
        return min(variants, key=lambda v: v.bandwidth or 0)
    if quality.isdigit():
        index = int(quality)
        if 0 <= index < len(variants):
            return variants[index]
        raise FatalError(f"variant index {index} out of range (0-{len(variants)-1})")
    for variant in variants:
        if quality in (variant.uri, variant.resolution):
            return variant
    raise FatalError(f"no variant matches {quality!r}")


# ----------------------------------------------------------------------
# downloading
# ----------------------------------------------------------------------
def _fetch_text(client: httpx.Client, url: str, timeout: float) -> tuple[str, str]:
    """Return (playlist text, final URL after redirects)."""
    try:
        response = client.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise FatalError(f"cannot fetch playlist {url}: {exc}") from exc
    if response.status_code != 200:
        raise FatalError(f"playlist {url} returned HTTP {response.status_code}")
    text = response.text
    if not text.lstrip().startswith("#EXTM3U"):
        raise FatalError(f"{url} is not an HLS playlist")
    return text, str(response.url)


def _fetch_key(
    client: httpx.Client, key: HlsKey, timeout: float
) -> bytes:
    if not key.uri:
        raise FatalError("AES-128 EXT-X-KEY is missing URI")
    try:
        response = client.get(key.uri, timeout=timeout)
    except httpx.HTTPError as exc:
        raise FatalError(f"cannot fetch HLS key {key.uri}: {exc}") from exc
    if response.status_code != 200:
        raise FatalError(
            f"HLS key {key.uri} returned HTTP {response.status_code}"
        )
    data = response.content
    if len(data) != 16:
        raise FatalError(
            f"HLS AES-128 key {key.uri} has {len(data)} bytes (expected 16)"
        )
    return data


def _decrypt_file(path: Path, key: bytes, iv: bytes) -> None:
    """Decrypt one AES-128-CBC segment in place (PKCS#7 padding)."""
    tmp = path.with_name(path.name + ".dec")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        unpadder = _padding.PKCS7(128).unpadder()
        with open(path, "rb") as source, open(tmp, "wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                target.write(unpadder.update(decryptor.update(chunk)))
            target.write(unpadder.update(decryptor.finalize()))
            target.write(unpadder.finalize())
        os.replace(tmp, path)
    except (ValueError, OSError) as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise FatalError(
            f"AES-128 decryption failed for {path.name}: {exc}"
        ) from exc


def _resolve_media(
    client: httpx.Client, url: str, quality: str | None, timeout: float
) -> MediaPlaylist:
    text, final_url = _fetch_text(client, url, timeout)
    playlist = parse_playlist(text, final_url)
    if playlist.is_master:
        variant = choose_variant(playlist.variants, quality)
        log.info("HLS variant: %s (%s)", variant.uri, variant.resolution or "-")
        text, final_url = _fetch_text(client, variant.uri, timeout)
        playlist = parse_playlist(text, final_url)
    if playlist.is_master:  # master pointing at master is not supported
        raise FatalError("nested master playlists are not supported")
    media = playlist.media
    assert media is not None
    if media.encryption_error:
        raise FatalError(media.encryption_error)
    if not media.ended:
        raise FatalError(
            "live/event HLS playlists are not supported (missing #EXT-X-ENDLIST)"
        )
    return media


def download_hls(
    config: DownloadConfig,
    quality: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> DownloadResult:
    """Download a VOD HLS stream into one file.

    ``cancel_event`` stops segment workers (they check it between chunks);
    ``progress_callback`` receives dicts shaped like the engine's progress
    snapshots, with ``total`` unknown until the file is assembled.
    """
    started = time.monotonic()
    governor = config.governor or default_governor()
    policy = governor.resolve(config.url)
    limits = [value for value in (config.speed_limit, policy.speed_limit) if value]
    bucket = TokenBucket(min(limits) if limits else None)

    # Playlists and segments can live on different sites (CDN).  Each host
    # gets its own client built from that host's rule, so site-specific
    # UA/Cookie/auth applies to every request, not just the playlist.
    clients: dict[tuple[str, str], httpx.Client] = {}
    clients_lock = threading.Lock()

    def client_for(url: str) -> httpx.Client:
        parts = urlsplit(url)
        key = (parts.scheme, parts.netloc)
        with clients_lock:
            client = clients.get(key)
            if client is None:
                request_config = apply_policy(config, governor.resolve(url))
                client = make_client(
                    request_config, max_connections=max(1, config.max_connections)
                )
                clients[key] = client
            return client

    output_text = config.output
    if not output_text:
        raise FatalError("an output path is required for HLS downloads")
    output = Path(str(output_text))
    if output.suffix.lower() not in (".ts", ".mp4", ".m4s", ".bin", ".mkv"):
        output = output.with_suffix(".ts")
    if output.exists() and not config.overwrite:
        output = unique_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    parts = Path(str(output) + ".hls.parts")
    parts.mkdir(parents=True, exist_ok=True)
    progress = _Progress()
    report_stop = threading.Event()
    reporter: threading.Thread | None = None

    try:
        media = _resolve_media(
            client_for(config.url), config.url, quality, config.timeout
        )
        key_cache: dict[str, bytes] = {}

        def key_bytes_for(key: HlsKey | None) -> bytes | None:
            if key is None:
                return None
            if not key.uri:
                raise FatalError("AES-128 EXT-X-KEY is missing URI")
            if key.uri not in key_cache:
                key_cache[key.uri] = _fetch_key(
                    client_for(key.uri), key, config.timeout
                )
            return key_cache[key.uri]

        total_duration = sum(max(0.0, seg.duration) for seg in media.segments)
        tasks: list[
            tuple[
                str,
                tuple[int, int] | None,
                Path,
                float,
                bytes | None,
                bytes | None,
            ]
        ] = []
        if media.init_uri:
            init_key = (
                key_bytes_for(media.init_key)
                if media.init_iv is not None
                else None
            )
            tasks.append(
                (
                    media.init_uri,
                    media.init_byterange,
                    parts / "00000-init.bin",
                    0.0,
                    init_key,
                    media.init_iv if init_key else None,
                )
            )
        for index, segment in enumerate(media.segments, start=1):
            key_bytes = key_bytes_for(segment.key)
            iv = None
            if key_bytes is not None:
                iv = (
                    segment.key.iv
                    if segment.key is not None and segment.key.iv is not None
                    else segment.sequence.to_bytes(16, "big")
                )
            tasks.append(
                (
                    segment.uri,
                    segment.byterange,
                    parts / f"{index:05d}.bin",
                    segment.duration,
                    key_bytes,
                    iv,
                )
            )

        if progress_callback is not None:
            reporter = threading.Thread(
                target=_report_loop,
                args=(
                    progress,
                    len(tasks),
                    total_duration,
                    started,
                    progress_callback,
                    report_stop,
                ),
                name="pdm-hls-progress",
                daemon=True,
            )
            reporter.start()

        def worker(task) -> None:
            url, byterange, target, duration, key_bytes, iv = task
            progress.enter()
            try:
                marker = (
                    target.with_name(target.name + ".ok")
                    if key_bytes is not None
                    else None
                )
                if marker is not None and marker.exists():
                    progress.segment_done(duration)
                    return
                if target.exists():
                    size = target.stat().st_size
                    if (
                        key_bytes is None
                        and (byterange is None or size == byterange[0])
                    ):
                        progress.segment_done(duration)
                        return
                    target.unlink(missing_ok=True)
                _download_segment(
                    client_for(url),
                    url,
                    byterange,
                    target,
                    config,
                    bucket,
                    governor,
                    progress,
                    cancel_event,
                    duration,
                )
                if key_bytes is not None:
                    assert iv is not None
                    _decrypt_file(target, key_bytes, iv)
                    if marker is not None:
                        marker.write_bytes(b"")
            finally:
                progress.leave()

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=max(1, config.max_connections),
            thread_name_prefix="pdm-hls",
        ) as pool:
            list(pool.map(worker, tasks))

        _check_cancel(cancel_event)
        part_file = Path(str(output) + ".hls.part")
        with open(part_file, "wb") as out:
            for _url, _range, target, _duration, _key, _iv in tasks:
                with open(target, "rb") as handle:
                    shutil.copyfileobj(handle, out, 1024 * 1024)
        part_file.replace(output)

        digest = None
        if config.sha256:
            hasher = hashlib.sha256()
            with open(output, "rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
            digest = hasher.hexdigest()
            if digest.lower() != config.sha256.lower():
                raise ChecksumMismatch(config.sha256, digest)

        elapsed = max(1e-9, time.monotonic() - started)
        total = output.stat().st_size
        return DownloadResult(
            url=config.url,
            final_url=media.url,
            path=output,
            total_size=total,
            downloaded=total,
            elapsed=elapsed,
            avg_speed=total / elapsed,
            sha256=digest,
            range_supported=True,
            resumed=False,
            segments_created=len(tasks),
            peak_connections=progress.peak,
            retries=progress.retries,
            final_connections=config.max_connections,
        )
    finally:
        report_stop.set()
        if reporter is not None:
            reporter.join(timeout=1.0)
        with clients_lock:
            owned = list(clients.values())
            clients.clear()
        for client in owned:
            try:
                client.close()
            except Exception:
                pass
        shutil.rmtree(parts, ignore_errors=True)
