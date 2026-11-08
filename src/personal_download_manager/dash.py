"""DASH (MPEG-DASH / MPD) VOD downloader, unencrypted content only.

Supported:
  * static (VOD) single-period MPDs;
  * ``SegmentTemplate`` with ``$Number$`` / ``$Time$`` / ``$RepresentationID$``
    / ``$Bandwidth$``, zero-padded tokens, ``@duration`` or ``SegmentTimeline``
    (including ``r=-1`` bounded by the next ``S@t`` or the period duration);
  * ``SegmentList`` with per-segment media URLs and an initialization URL;
  * nested ``BaseURL`` resolution and relative media URLs;
  * one track at a time (video or audio), or ``track_kind="both"`` to fetch
    both and optionally mux them with an external ``ffmpeg -c copy``.  Without
    ffmpeg the two tracks are kept as separate files with an explicit warning.

Deliberately *not* supported: ``type="dynamic"`` (live), multiple periods,
encrypted content (``ContentProtection``), and ``SegmentBase``/``indexRange``
single-file MP4 layouts (they need SIDX parsing).  Those fail with a clear
error instead of producing a corrupt file.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .client import apply_policy, make_client
from .errors import Cancelled, ChecksumMismatch, FatalError
from .headers import unique_path
from .models import DownloadConfig, DownloadResult
from .policy import default_governor
from .ratelimit import TokenBucket
from .streaming import (
    StreamProgress,
    check_cancel,
    download_stream_segment,
    report_loop,
)

log = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"\$Number(?:%0(\d+)d)?\$")
_TIME_RE = re.compile(r"\$Time(?:%0(\d+)d)?\$")
_DURATION_RE = re.compile(
    r"^P(?:(\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


def is_dash_url(url: str) -> bool:
    """Heuristic used by the GUI to route MPD URLs to the DASH engine."""
    try:
        return urlsplit(url).path.lower().endswith(".mpd")
    except ValueError:
        return False


# ----------------------------------------------------------------------
# small XML helpers (namespace-agnostic)
# ----------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _has_descendant(element: ET.Element, name: str) -> bool:
    return any(_local(node.tag) == name for node in element.iter())


def parse_duration(value: str | None) -> float | None:
    """Parse an ISO-8601 duration such as ``PT1H2M3.5S`` into seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise FatalError(f"invalid ISO-8601 duration: {value!r}")
    days, hours, minutes, seconds = (float(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_range(value: str | None) -> tuple[int, int] | None:
    """``"start-end"`` -> ``(offset, length)``."""
    if not value:
        return None
    text = value.strip()
    if "-" not in text:
        raise FatalError(f"invalid byte range: {value!r}")
    start_s, end_s = text.split("-", 1)
    try:
        start = int(start_s)
        end = int(end_s)
    except ValueError as exc:
        raise FatalError(f"invalid byte range: {value!r}") from exc
    if end < start:
        raise FatalError(f"invalid byte range: {value!r}")
    return start, end - start + 1


# ----------------------------------------------------------------------
# manifest model
# ----------------------------------------------------------------------
@dataclass
class DashSegment:
    url: str
    byterange: tuple[int, int] | None = None  # (offset, length)
    duration: float = 0.0


@dataclass
class DashInitialization:
    """An initialization segment and the media-segment index it precedes."""

    url: str
    byterange: tuple[int, int] | None = None
    position: int = 0


@dataclass
class SegmentBaseInfo:
    """Single-file SegmentBase/indexRange representation (resolved lazily)."""

    file_url: str
    index_range: tuple[int, int] | None = None
    init_url: str | None = None
    init_range: tuple[int, int] | None = None
    timescale: int = 1
    presentation_time_offset: int = 0


@dataclass
class DashTrack:
    kind: str
    bandwidth: int = 0
    width: int | None = None
    height: int | None = None
    mime: str | None = None
    codecs: str | None = None
    segments: list[DashSegment] = field(default_factory=list)
    init_url: str | None = None
    init_range: tuple[int, int] | None = None
    initializations: list[DashInitialization] = field(default_factory=list)
    duration: float = 0.0
    representation_id: str = ""
    encrypted: bool = False
    period_index: int = 0
    segment_base: SegmentBaseInfo | None = None


@dataclass
class DashManifest:
    url: str
    type: str
    duration: float | None
    tracks: list[DashTrack]
    period_count: int


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def parse_mpd(text: str, base_url: str) -> DashManifest:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FatalError(f"invalid MPD XML: {exc}") from exc
    if _local(root.tag) != "MPD":
        raise FatalError("not an MPD document (root element is not <MPD>)")

    mpd_type = (root.get("type") or "static").strip().lower()
    if mpd_type != "static":
        raise FatalError(
            "live/dynamic DASH (MPD@type != static) is not supported"
        )

    duration = parse_duration(root.get("mediaPresentationDuration"))
    periods = _children(root, "Period")
    if not periods:
        raise FatalError("MPD has no <Period>")
    period_starts, period_durations = _period_timing(periods, duration)

    base = base_url
    mpd_base = _child(root, "BaseURL")
    if mpd_base is not None and (mpd_base.text or "").strip():
        base = urljoin(base, mpd_base.text.strip())

    tracks: list[DashTrack] = []
    for period_index, period in enumerate(periods):
        period_duration = period_durations[period_index]
        period_base_url = base
        period_base = _child(period, "BaseURL")
        if period_base is not None and (period_base.text or "").strip():
            period_base_url = urljoin(period_base_url, period_base.text.strip())

        period_template = _child(period, "SegmentTemplate")
        period_list = _child(period, "SegmentList")
        period_segment_base = _child(period, "SegmentBase")

        for adaptation in _children(period, "AdaptationSet"):
            adapt_base = period_base_url
            adapt_url = _child(adaptation, "BaseURL")
            if adapt_url is not None and (adapt_url.text or "").strip():
                adapt_base = urljoin(adapt_base, adapt_url.text.strip())
            adapt_encrypted = _has_descendant(adaptation, "ContentProtection")
            adapt_template = _child(adaptation, "SegmentTemplate")
            adapt_list = _child(adaptation, "SegmentList")
            adapt_segment_base = _child(adaptation, "SegmentBase")

            for representation in _children(adaptation, "Representation"):
                rep_base = adapt_base
                rep_url = _child(representation, "BaseURL")
                has_base = bool(
                    (period_base is not None and (period_base.text or "").strip())
                    or (adapt_url is not None and (adapt_url.text or "").strip())
                    or (rep_url is not None and (rep_url.text or "").strip())
                )
                if rep_url is not None and (rep_url.text or "").strip():
                    rep_base = urljoin(rep_base, rep_url.text.strip())

                kind = _track_kind(adaptation, representation)
                if kind not in ("video", "audio"):
                    continue

                rep_template = _child(representation, "SegmentTemplate")
                rep_list = _child(representation, "SegmentList")
                track = DashTrack(
                    kind=kind,
                    bandwidth=_int(representation.get("bandwidth")),
                    width=_int(representation.get("width"))
                    or _int(adaptation.get("width")),
                    height=_int(representation.get("height"))
                    or _int(adaptation.get("height")),
                    mime=representation.get("mimeType")
                    or adaptation.get("mimeType"),
                    codecs=representation.get("codecs")
                    or adaptation.get("codecs"),
                    duration=period_duration or 0.0,
                    representation_id=representation.get("id") or "",
                    encrypted=adapt_encrypted
                    or _has_descendant(representation, "ContentProtection"),
                    period_index=period_index,
                )

                segment_base = _child(representation, "SegmentBase")
                if segment_base is None:
                    segment_base = adapt_segment_base
                if segment_base is None:
                    segment_base = period_segment_base
                if segment_base is not None:
                    if not has_base:
                        raise FatalError(
                            f"representation {track.representation_id!r} uses "
                            "SegmentBase without a BaseURL"
                        )
                    init = _child(segment_base, "Initialization")
                    init_url = None
                    init_range = None
                    if init is not None:
                        source = (init.get("sourceURL") or "").strip()
                        init_url = (
                            urljoin(rep_base, source) if source else rep_base
                        )
                        init_range = parse_range(init.get("range"))
                    track.segment_base = SegmentBaseInfo(
                        file_url=rep_base,
                        index_range=parse_range(segment_base.get("indexRange")),
                        init_url=init_url,
                        init_range=init_range,
                        timescale=_int(segment_base.get("timescale")) or 1,
                        presentation_time_offset=_int(
                            segment_base.get("presentationTimeOffset")
                        ),
                    )
                    track.init_url = init_url
                    track.init_range = init_range
                    if init_url is not None:
                        track.initializations = [
                            DashInitialization(init_url, init_range, 0)
                        ]
                    tracks.append(track)
                    continue

                if (
                    rep_template is not None
                    or adapt_template is not None
                    or period_template is not None
                ):
                    track.segments, track.init_url, track.init_range = _template_track(
                        representation,
                        rep_template,
                        adapt_template,
                        period_template,
                        rep_base,
                        period_duration,
                        track,
                    )
                elif (
                    rep_list is not None
                    or adapt_list is not None
                    or period_list is not None
                ):
                    chosen_list = (
                        rep_list
                        if rep_list is not None
                        else (adapt_list if adapt_list is not None else period_list)
                    )
                    track.segments, track.init_url, track.init_range = _list_track(
                        chosen_list, rep_base, track
                    )
                elif has_base:
                    # A Representation that is just one progressive file.
                    track.segments = [
                        DashSegment(rep_base, duration=track.duration)
                    ]
                else:
                    raise FatalError(
                        f"representation {track.representation_id!r} has no segment "
                        "information (expected SegmentTemplate/SegmentList)"
                    )
                if not track.segments:
                    raise FatalError(
                        f"representation {track.representation_id!r} has no segments"
                    )
                if track.init_url is not None:
                    track.initializations = [
                        DashInitialization(
                            track.init_url, track.init_range, 0
                        )
                    ]
                tracks.append(track)

    if not tracks:
        raise FatalError("MPD has no video or audio representations")
    total_duration = duration
    if total_duration is None:
        known = [item for item in period_durations if item is not None]
        total_duration = sum(known) if known else None
    return DashManifest(
        url=base_url,
        type=mpd_type,
        duration=total_duration,
        tracks=tracks,
        period_count=len(periods),
    )


def _period_timing(
    periods: list[ET.Element], mpd_duration: float | None
) -> tuple[list[float], list[float | None]]:
    """Derive each Period's start and duration from start/duration/MPD hints."""
    raw_starts = [parse_duration(period.get("start")) for period in periods]
    raw_durations = [parse_duration(period.get("duration")) for period in periods]
    starts: list[float] = []
    durations: list[float | None] = []
    current = 0.0
    for index, _period in enumerate(periods):
        start = raw_starts[index] if raw_starts[index] is not None else current
        period_duration = raw_durations[index]
        if period_duration is None:
            next_start = next(
                (
                    raw_starts[later]
                    for later in range(index + 1, len(periods))
                    if raw_starts[later] is not None
                ),
                None,
            )
            if next_start is not None:
                period_duration = max(0.0, next_start - start)
            elif mpd_duration is not None:
                period_duration = max(0.0, mpd_duration - start)
        starts.append(start)
        durations.append(period_duration)
        current = start + (period_duration or 0.0)
    return starts, durations


def _int(value: str | None) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _track_kind(adaptation: ET.Element, representation: ET.Element) -> str:
    content = (adaptation.get("contentType") or "").strip().lower()
    if content in ("video", "audio"):
        return content
    mime = (
        representation.get("mimeType")
        or adaptation.get("mimeType")
        or ""
    ).lower()
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if representation.get("height") or adaptation.get("height"):
        return "video"
    if representation.get("audioSamplingRate") or adaptation.get(
        "audioSamplingRate"
    ):
        return "audio"
    return ""


def _merge_template(
    representation: ET.Element,
    rep_template: ET.Element | None,
    adapt_template: ET.Element | None,
    period_template: ET.Element | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    for element in (period_template, adapt_template, rep_template):
        if element is not None:
            merged.update(element.attrib)
    return merged


def _timeline_of(
    rep_template: ET.Element | None,
    adapt_template: ET.Element | None,
    period_template: ET.Element | None = None,
) -> ET.Element | None:
    for element in (rep_template, adapt_template, period_template):
        if element is not None:
            timeline = _child(element, "SegmentTimeline")
            if timeline is not None:
                return timeline
    return None


def _template_track(
    representation: ET.Element,
    rep_template: ET.Element | None,
    adapt_template: ET.Element | None,
    period_template: ET.Element | None,
    base: str,
    period_duration: float | None,
    track: DashTrack,
) -> tuple[list[DashSegment], str | None, tuple[int, int] | None]:
    attrs = _merge_template(
        representation, rep_template, adapt_template, period_template
    )
    media = attrs.get("media")
    if not media:
        raise FatalError("SegmentTemplate has no @media")

    timescale = _int(attrs.get("timescale")) or 1
    start_number = _int(attrs.get("startNumber")) or 1
    pto = _int(attrs.get("presentationTimeOffset"))
    timeline = _timeline_of(rep_template, adapt_template, period_template)

    segments: list[DashSegment]
    if timeline is not None:
        segments = _timeline_segments(
            timeline, media, representation, track, timescale, start_number
        )
    else:
        duration = _int(attrs.get("duration"))
        if duration <= 0:
            raise FatalError(
                "SegmentTemplate needs @duration or a SegmentTimeline"
            )
        if not period_duration:
            raise FatalError(
                "manifest has no mediaPresentationDuration; cannot expand a "
                "duration-based SegmentTemplate"
            )
        total = max(0, int(math.ceil(period_duration * timescale)) - pto)
        count = int(math.ceil(total / duration)) if total > 0 else 0
        if count <= 0:
            raise FatalError("SegmentTemplate expands to zero segments")
        segments = []
        for index in range(count):
            number = start_number + index
            seg_time = pto + index * duration
            segments.append(
                DashSegment(
                    _expand_url(media, representation, number, seg_time),
                    duration=duration / timescale,
                )
            )

    init_url = None
    init_range = None
    if attrs.get("initialization"):
        init_url = _expand_url(
            attrs["initialization"], representation, start_number, pto
        )
        init_url = urljoin(base, init_url)
        init_range = parse_range(attrs.get("initializationRange"))
    for segment in segments:
        segment.url = urljoin(base, segment.url)
    return segments, init_url, init_range


def _timeline_segments(
    timeline: ET.Element,
    media: str,
    representation: ET.Element,
    track: DashTrack,
    timescale: int,
    start_number: int,
) -> list[DashSegment]:
    entries = _children(timeline, "S")
    if not entries:
        raise FatalError("SegmentTimeline has no <S> entries")
    segments: list[DashSegment] = []
    number = start_number
    current = 0
    for index, entry in enumerate(entries):
        start = _int(entry.get("t")) if entry.get("t") is not None else current
        duration = _int(entry.get("d"))
        if duration <= 0:
            raise FatalError("SegmentTimeline entry has no positive @d")
        repeat = _int(entry.get("r")) if entry.get("r") is not None else 0
        if repeat < 0:
            upcoming = None
            if index + 1 < len(entries):
                upcoming = entries[index + 1].get("t")
            if upcoming is not None:
                until = _int(upcoming)
                repeats = max(0, int(math.ceil((until - start) / duration)) - 1)
            elif track.duration:
                until = int(round(track.duration * timescale))
                repeats = max(0, int(math.ceil((until - start) / duration)) - 1)
            else:
                raise FatalError(
                    "SegmentTimeline r=-1 without a following S@t or a known "
                    "period duration"
                )
        else:
            repeats = repeat
        for step in range(repeats + 1):
            seg_time = start + step * duration
            segments.append(
                DashSegment(
                    _expand_url(media, representation, number, seg_time),
                    duration=duration / timescale,
                )
            )
            number += 1
        current = start + (repeats + 1) * duration
    return segments


def _list_track(
    segment_list: ET.Element,
    base: str,
    track: DashTrack,
) -> tuple[list[DashSegment], str | None, tuple[int, int] | None]:
    timescale = _int(segment_list.get("timescale")) or 1
    duration = _int(segment_list.get("duration"))
    segment_duration = duration / timescale if duration else 0.0

    init_url = None
    init_range = None
    initialization = _child(segment_list, "Initialization")
    if initialization is not None and initialization.get("sourceURL"):
        init_url = urljoin(base, initialization.get("sourceURL", ""))
        init_range = parse_range(initialization.get("range"))

    segments: list[DashSegment] = []
    for item in _children(segment_list, "SegmentURL"):
        media = item.get("media")
        if not media:
            raise FatalError("SegmentURL without @media is not supported")
        segments.append(
            DashSegment(
                urljoin(base, media),
                byterange=parse_range(item.get("mediaRange")),
                duration=segment_duration,
            )
        )
    return segments, init_url, init_range


def _expand_url(
    template: str,
    representation: ET.Element,
    number: int | None,
    seg_time: int | None,
) -> str:
    text = template.replace("$$", "\x00")
    text = text.replace("$RepresentationID$", representation.get("id") or "")
    text = text.replace("$Bandwidth$", representation.get("bandwidth") or "0")
    if number is not None:
        text = _NUMBER_RE.sub(
            lambda match: _pad(number, match.group(1)), text
        )
    if seg_time is not None:
        text = _TIME_RE.sub(
            lambda match: _pad(seg_time, match.group(1)), text
        )
    return text.replace("\x00", "$")


def _pad(value: int, width: str | None) -> str:
    if width:
        return str(value).zfill(int(width))
    return str(value)


# ----------------------------------------------------------------------
# track selection
# ----------------------------------------------------------------------
def _choose_candidate(
    candidates: list[DashTrack], kind: str, quality: str | None
) -> DashTrack:
    candidates.sort(key=lambda track: track.bandwidth)
    if quality is None or quality in ("best", "auto"):
        return candidates[-1]
    if quality == "worst":
        return candidates[0]
    if quality.isdigit():
        wanted = int(quality)
        for track in candidates:
            if track.height == wanted or track.width == wanted:
                return track
        if 0 <= wanted < len(candidates):
            return candidates[wanted]
        raise FatalError(
            f"track index {wanted} out of range (0-{len(candidates) - 1})"
        )
    for track in candidates:
        if quality in (track.representation_id, str(track.height)):
            return track
    raise FatalError(f"no {kind} track matches {quality!r}")


def choose_track(
    tracks: list[DashTrack], kind: str = "video", quality: str | None = None
) -> DashTrack:
    candidates = [track for track in tracks if track.kind == kind]
    if not candidates:
        available = ", ".join(sorted({track.kind for track in tracks}))
        raise FatalError(
            f"MPD has no {kind} track (available: {available or 'none'})"
        )
    return _choose_candidate(candidates, kind, quality)


def select_tracks(
    manifest: DashManifest, kind: str = "video", quality: str | None = None
) -> list[DashTrack]:
    """Pick one track per Period (multi-period DASH) for a single output stream."""
    grouped: dict[int, list[DashTrack]] = {}
    for track in manifest.tracks:
        if track.kind == kind:
            grouped.setdefault(track.period_index, []).append(track)
    if not grouped:
        available = ", ".join(sorted({track.kind for track in manifest.tracks}))
        raise FatalError(
            f"MPD has no {kind} track (available: {available or 'none'})"
        )
    selected: list[DashTrack] = []
    for period_index in sorted(grouped):
        candidates = grouped[period_index]
        try:
            selected.append(_choose_candidate(candidates, kind, quality))
        except FatalError:
            # A period without the requested quality/kind cannot be represented;
            # skipping keeps a 2-period video + 1-period audio manifest usable.
            if quality in (None, "best", "auto", "worst"):
                log.warning(
                    "DASH period %s has no %s track; skipping it",
                    period_index,
                    kind,
                )
                continue
            raise
    if not selected:
        raise FatalError(f"MPD has no usable {kind} track")
    return selected


def combine_tracks(tracks: list[DashTrack]) -> DashTrack:
    """Flatten selected per-Period tracks into one ordered download track."""
    first = tracks[0]
    segments: list[DashSegment] = []
    initializations: list[DashInitialization] = []
    for track in tracks:
        base_position = len(segments)
        track_inits = track.initializations
        if not track_inits and track.init_url is not None:
            track_inits = [
                DashInitialization(track.init_url, track.init_range, 0)
            ]
        for init in track_inits:
            initializations.append(
                DashInitialization(
                    init.url, init.byterange, base_position + init.position
                )
            )
        segments.extend(track.segments)
    duration = sum(track.duration for track in tracks)
    return replace(
        first,
        segments=segments,
        initializations=initializations,
        init_url=initializations[0].url if initializations else first.init_url,
        init_range=(
            initializations[0].byterange
            if initializations
            else first.init_range
        ),
        duration=duration,
        encrypted=any(track.encrypted for track in tracks),
    )


# ----------------------------------------------------------------------
# downloading
# ----------------------------------------------------------------------
@dataclass
class _Task:
    url: str
    byterange: tuple[int, int] | None  # (offset, length) as in the manifest
    target: Path
    duration: float = 0.0


def _fetch_mpd(client: httpx.Client, url: str, timeout: float) -> tuple[str, str]:
    try:
        response = client.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise FatalError(f"cannot fetch MPD {url}: {exc}") from exc
    if response.status_code != 200:
        raise FatalError(f"MPD {url} returned HTTP {response.status_code}")
    return response.text, str(response.url)


def _output_path(config: DownloadConfig, kind: str) -> Path:
    raw = config.output
    if not raw:
        raise FatalError("an output path is required for DASH downloads")
    output = Path(str(raw))
    allowed = (".mp4", ".m4v", ".mkv") if kind == "video" else (".m4a", ".mp4", ".aac")
    if output.suffix.lower() not in allowed:
        output = output.with_suffix(".mp4" if kind == "video" else ".m4a")
    if output.exists() and not config.overwrite:
        output = unique_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _range_for(template_range: tuple[int, int] | None):
    """(offset, length) -> the (length, offset) shape streaming expects."""
    if template_range is None:
        return None
    offset, length = template_range
    return length, offset


FFMPEG_ENV = "PDM_DOWNLOADER_FFMPEG"


def find_ffmpeg(config: DownloadConfig) -> str | None:
    """Resolve the optional ffmpeg executable.

    Order: ``config.ffmpeg_path`` / ``PDM_DOWNLOADER_FFMPEG`` (a file, a directory,
    or a command name), then ``ffmpeg`` on PATH.  Returns None when no
    executable is configured or found; an explicitly configured but missing
    path is an error so typos do not silently fall back to two files.
    """
    configured = (config.ffmpeg_path or os.environ.get(FFMPEG_ENV) or "").strip()
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
        raise FatalError(f"ffmpeg not found in directory {path}")
    if path.is_file():
        return str(path)

    found = shutil.which(configured)
    if found:
        return found
    raise FatalError(f"ffmpeg not found at {path}")


def _mux_command(
    ffmpeg: str, video: Path, audio: Path, output: Path
) -> list[str]:
    """Build ``ffmpeg -c copy`` for fragmented MP4 video + audio tracks."""
    command = [
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
    ]
    # faststart is an MP4/MOV concept; mkv/aac outputs do not accept it.
    if output.suffix.lower() in (".mp4", ".m4v", ".mov"):
        command += ["-movflags", "+faststart"]
    command.append(str(output))
    return command


def _run_ffmpeg(command: list[str]) -> None:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **kwargs,
        )
    except OSError as exc:
        raise FatalError(f"cannot run ffmpeg: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FatalError("ffmpeg timed out after 3600s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > 2000:
            detail = "..." + detail[-2000:]
        raise FatalError(
            f"ffmpeg failed with exit code {completed.returncode}: {detail}"
        )


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


# ----------------------------------------------------------------------
# SegmentBase / SIDX resolution
# ----------------------------------------------------------------------
def _box_header(data: bytes, pos: int) -> tuple[int, bytes, int]:
    if pos + 8 > len(data):
        raise FatalError("truncated ISO BMFF box header")
    size = int.from_bytes(data[pos : pos + 4], "big")
    box_type = data[pos + 4 : pos + 8]
    header = 8
    if size == 1:
        if pos + 16 > len(data):
            raise FatalError("truncated ISO BMFF largesize")
        size = int.from_bytes(data[pos + 8 : pos + 16], "big")
        header = 16
    elif size == 0:
        size = len(data) - pos
    if size < header or pos + size > len(data):
        raise FatalError("invalid ISO BMFF box size")
    return size, box_type, header


def parse_sidx(data: bytes, index_start: int = 0) -> list[DashSegment]:
    """Parse top-level SIDX boxes into (absolute offset, size, duration) segments.

    The input is the byte range returned for SegmentBase@indexRange.  Hierarchical
    index references (reference_type=1) are rejected explicitly rather than being
    treated as media bytes.
    """
    segments: list[DashSegment] = []
    pos = 0
    while pos + 8 <= len(data):
        size, box_type, header = _box_header(data, pos)
        box = data[pos : pos + size]
        if box_type == b"sidx":
            segments.extend(
                _parse_sidx_box(box, index_start + pos, header)
            )
        pos += size
    if not segments:
        raise FatalError("segment index contained no SIDX media references")
    return segments


def _parse_sidx_box(
    box: bytes, absolute_offset: int, header: int
) -> list[DashSegment]:
    if len(box) < header + 4:
        raise FatalError("truncated SIDX box")
    p = header
    version = box[p]
    p += 4  # version + flags
    if p + 8 > len(box):
        raise FatalError("truncated SIDX header")
    p += 4  # reference_ID
    timescale = int.from_bytes(box[p : p + 4], "big")
    p += 4
    if timescale <= 0:
        raise FatalError("SIDX timescale must be positive")
    if version == 0:
        p += 8  # earliest_presentation_time + first_offset (32-bit each)
        first_offset = int.from_bytes(box[p - 4 : p], "big")
    else:
        p += 16  # 64-bit earliest_presentation_time + first_offset
        first_offset = int.from_bytes(box[p - 8 : p], "big")
    p += 2  # reserved
    if p + 2 > len(box):
        raise FatalError("truncated SIDX reference count")
    reference_count = int.from_bytes(box[p : p + 2], "big")
    p += 2
    media_offset = absolute_offset + len(box) + first_offset
    segments: list[DashSegment] = []
    for _ in range(reference_count):
        if p + 12 > len(box):
            raise FatalError("truncated SIDX reference entry")
        word = int.from_bytes(box[p : p + 4], "big")
        reference_type = word >> 31
        referenced_size = word & 0x7FFFFFFF
        p += 4
        duration = int.from_bytes(box[p : p + 4], "big")
        p += 4
        p += 4  # SAP
        if referenced_size <= 0:
            raise FatalError("SIDX reference has zero size")
        if reference_type == 1:
            raise FatalError(
                "hierarchical SIDX index references are not supported"
            )
        segments.append(
            DashSegment(
                url="",  # filled by the caller (same file URL)
                byterange=(media_offset, referenced_size),
                duration=duration / timescale,
            )
        )
        media_offset += referenced_size
    return segments


def _fetch_range(
    client: httpx.Client, url: str, byterange: tuple[int, int], timeout: float
) -> bytes:
    offset, length = byterange
    if length <= 0:
        raise FatalError("empty byte range")
    end = offset + length - 1
    try:
        response = client.get(
            url, headers={"Range": f"bytes={offset}-{end}"}, timeout=timeout
        )
    except httpx.HTTPError as exc:
        raise FatalError(f"cannot fetch SIDX from {url}: {exc}") from exc
    if response.status_code == 206:
        data = response.content
    elif response.status_code == 200:
        data = response.content[offset : offset + length]
    else:
        raise FatalError(
            f"SIDX request {url} returned HTTP {response.status_code}"
        )
    if len(data) != length:
        raise FatalError(
            f"SIDX request {url} returned {len(data)} bytes, expected {length}"
        )
    return data


def _resolve_segment_base(
    client: httpx.Client, track: DashTrack, timeout: float
) -> None:
    sb = track.segment_base
    if sb is None:
        return
    if sb.index_range is None:
        # No index: the BaseURL is a complete progressive MP4.
        track.segments = [DashSegment(sb.file_url, duration=track.duration)]
        if sb.init_url and sb.init_url != sb.file_url:
            track.initializations = [
                DashInitialization(sb.init_url, sb.init_range, 0)
            ]
        track.segment_base = None
        return
    index = _fetch_range(client, sb.file_url, sb.index_range, timeout)
    segments = parse_sidx(index, sb.index_range[0])
    for segment in segments:
        segment.url = sb.file_url
    track.segments = segments
    if sb.init_url and (sb.init_range is not None or sb.init_url != sb.file_url):
        track.initializations = [
            DashInitialization(sb.init_url, sb.init_range, 0)
        ]
    log.info("DASH SegmentBase: %d SIDX segments", len(segments))
    track.segment_base = None


def download_dash(
    config: DownloadConfig,
    quality: str | None = None,
    track_kind: str | None = None,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> DownloadResult:
    """Download one DASH track, or both tracks with optional ffmpeg merging."""
    kind = (track_kind or "video").strip().lower()
    if kind == "both":
        return _download_both(
            config,
            quality,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
    if kind not in ("video", "audio"):
        raise FatalError("DASH track must be 'video', 'audio' or 'both'")
    return _download_one_track(
        config,
        quality,
        kind,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
    )


def _download_both(
    config: DownloadConfig,
    quality: str | None,
    *,
    cancel_event: threading.Event | None,
    progress_callback: Callable[[dict], None] | None,
) -> DownloadResult:
    """Download video + audio sequentially, then mux when ffmpeg is available."""
    started = time.monotonic()
    ffmpeg = find_ffmpeg(config)
    if config.sha256 and not ffmpeg:
        raise FatalError(
            "DASH dual-track SHA-256 verification needs ffmpeg; configure "
            "--ffmpeg or use --track video/audio"
        )

    output = _output_path(config, "video")
    audio = output.with_name(output.stem + ".audio.m4a")
    if audio.exists() and not config.overwrite:
        audio = unique_path(audio)

    video_size = 0
    user_callback = progress_callback

    def video_progress(snapshot: dict) -> None:
        if user_callback is None:
            return
        item = dict(snapshot)
        item["fraction"] = min(1.0, float(item.get("fraction") or 0.0)) * 0.5
        user_callback(item)

    def audio_progress(snapshot: dict) -> None:
        if user_callback is None:
            return
        item = dict(snapshot)
        item["fraction"] = 0.5 + min(1.0, float(item.get("fraction") or 0.0)) * 0.5
        try:
            item["downloaded"] = video_size + int(item.get("downloaded") or 0)
        except (TypeError, ValueError):
            pass
        user_callback(item)

    check_cancel(cancel_event)
    video_config = replace(config, output=output, sha256=None, overwrite=True)
    video = _download_one_track(
        video_config,
        quality,
        "video",
        cancel_event=cancel_event,
        progress_callback=video_progress if user_callback else None,
    )
    video_size = video.total_size
    check_cancel(cancel_event)

    audio_config = replace(config, output=audio, sha256=None, overwrite=True)
    audio_result = _download_one_track(
        audio_config,
        quality,
        "audio",
        cancel_event=cancel_event,
        progress_callback=audio_progress if user_callback else None,
    )
    check_cancel(cancel_event)

    if ffmpeg is None:
        log.warning(
            "ffmpeg not found; DASH tracks kept separately: %s and %s",
            output,
            audio,
        )
        return DownloadResult(
            url=config.url,
            final_url=video.final_url,
            path=output,
            total_size=video.total_size,
            downloaded=video.downloaded,
            elapsed=max(1e-9, time.monotonic() - started),
            avg_speed=video.avg_speed,
            sha256=None,
            range_supported=True,
            resumed=video.resumed or audio_result.resumed,
            segments_created=video.segments_created + audio_result.segments_created,
            peak_connections=max(
                video.peak_connections, audio_result.peak_connections
            ),
            retries=video.retries + audio_result.retries,
            final_connections=max(
                video.final_connections, audio_result.final_connections
            ),
            extra_paths=[str(audio)],
        )

    merged = output.with_name(output.stem + ".muxed" + output.suffix)
    command = _mux_command(ffmpeg, output, audio, merged)
    try:
        _run_ffmpeg(command)
        check_cancel(cancel_event)
        if not merged.exists() or merged.stat().st_size <= 0:
            raise FatalError("ffmpeg finished but produced no output file")
        merged.replace(output)
        audio.unlink(missing_ok=True)
    except BaseException:
        merged.unlink(missing_ok=True)
        raise

    digest = None
    if config.sha256:
        digest = _sha256_file(output)
        if digest.lower() != config.sha256.lower():
            raise ChecksumMismatch(config.sha256, digest)

    total = output.stat().st_size
    elapsed = max(1e-9, time.monotonic() - started)
    return DownloadResult(
        url=config.url,
        final_url=video.final_url,
        path=output,
        total_size=total,
        downloaded=total,
        elapsed=elapsed,
        avg_speed=total / elapsed,
        sha256=digest,
        range_supported=True,
        resumed=video.resumed or audio_result.resumed,
        segments_created=video.segments_created + audio_result.segments_created,
        peak_connections=max(video.peak_connections, audio_result.peak_connections),
        retries=video.retries + audio_result.retries,
        final_connections=max(video.final_connections, audio_result.final_connections),
    )


def _download_one_track(
    config: DownloadConfig,
    quality: str | None,
    track_kind: str,
    *,
    cancel_event: threading.Event | None,
    progress_callback: Callable[[dict], None] | None,
) -> DownloadResult:
    """Download one unencrypted DASH track into a single fragmented MP4."""
    started = time.monotonic()
    governor = config.governor or default_governor()
    policy = governor.resolve(config.url)
    limits = [value for value in (config.speed_limit, policy.speed_limit) if value]
    bucket = TokenBucket(min(limits) if limits else None)

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

    progress = StreamProgress()
    report_stop = threading.Event()
    reporter: threading.Thread | None = None
    parts_dir: Path | None = None
    try:
        text, final_url = _fetch_mpd(client_for(config.url), config.url, config.timeout)
        manifest = parse_mpd(text, final_url)
        kind = (track_kind or "video").strip().lower()
        if kind not in ("video", "audio"):
            raise FatalError("DASH track must be 'video' or 'audio'")
        selected = select_tracks(manifest, kind, quality)
        for item in selected:
            if item.encrypted:
                raise FatalError(
                    "encrypted DASH (ContentProtection) is not supported; this "
                    "project does not handle DRM or key retrieval"
                )
            if item.segment_base is not None:
                _resolve_segment_base(
                    client_for(item.segment_base.file_url),
                    item,
                    config.timeout,
                )
        track = combine_tracks(selected)
        if not track.segments:
            raise FatalError(f"DASH {kind} track has no segments")
        log.info(
            "DASH track: %s %s %s (%d period(s), %d segments)",
            kind,
            track.representation_id or "-",
            f"{track.width}x{track.height}" if track.width and track.height else "-",
            len(selected),
            len(track.segments),
        )

        output = _output_path(config, kind)
        parts_dir = Path(str(output) + ".dash.parts")
        parts_dir.mkdir(parents=True, exist_ok=True)

        initializations = track.initializations
        if not initializations and track.init_url is not None:
            initializations = [
                DashInitialization(track.init_url, track.init_range, 0)
            ]
        inits_by_position: dict[int, list[DashInitialization]] = {}
        for init in initializations:
            inits_by_position.setdefault(init.position, []).append(init)

        tasks: list[_Task] = []
        task_number = 0

        def add_init(init: DashInitialization) -> None:
            nonlocal task_number
            tasks.append(
                _Task(
                    init.url,
                    init.byterange,
                    parts_dir / f"{task_number:05d}-init.bin",
                )
            )
            task_number += 1

        for init in inits_by_position.get(0, []):
            add_init(init)
        for index, segment in enumerate(track.segments):
            if index:
                for init in inits_by_position.get(index, []):
                    add_init(init)
            tasks.append(
                _Task(
                    segment.url,
                    segment.byterange,
                    parts_dir / f"{task_number:05d}.bin",
                    segment.duration,
                )
            )
            task_number += 1
        for init in inits_by_position.get(len(track.segments), []):
            add_init(init)

        if progress_callback is not None:
            total_duration = sum(task.duration for task in tasks)
            reporter = threading.Thread(
                target=report_loop,
                args=(
                    progress,
                    len(tasks),
                    total_duration,
                    started,
                    progress_callback,
                    report_stop,
                ),
                name="pdm-dash-progress",
                daemon=True,
            )
            reporter.start()

        def worker(task: _Task) -> None:
            progress.enter()
            try:
                if task.target.exists():
                    size = task.target.stat().st_size
                    if task.byterange is None or size == task.byterange[1]:
                        progress.segment_done(task.duration)
                        return
                    task.target.unlink(missing_ok=True)
                download_stream_segment(
                    client_for(task.url),
                    task.url,
                    _range_for(task.byterange),
                    task.target,
                    config,
                    bucket,
                    governor,
                    progress,
                    cancel_event,
                    task.duration,
                )
            finally:
                progress.leave()

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(
            max_workers=max(1, config.max_connections),
            thread_name_prefix="pdm-dash",
        ) as pool:
            list(pool.map(worker, tasks))

        check_cancel(cancel_event)
        part_file = Path(str(output) + ".dash.part")
        with open(part_file, "wb") as out:
            for task in tasks:
                with open(task.target, "rb") as handle:
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
            final_url=final_url,
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
        if parts_dir is not None:
            shutil.rmtree(parts_dir, ignore_errors=True)
