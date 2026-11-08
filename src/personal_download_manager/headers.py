"""HTTP header parsing and filename handling.

Everything here is pure (no network, no filesystem writes) so it is cheap to
unit-test, and ``Content-Range`` / ``Content-Disposition`` handling is worth
pinning down precisely.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlsplit

_CONTENT_RANGE_RE = re.compile(
    r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.IGNORECASE
)
_UNSATISFIED_RANGE_RE = re.compile(r"^\s*bytes\s+\*/(\d+)\s*$", re.IGNORECASE)
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ContentRange:
    start: int
    end: int  # inclusive, as sent on the wire
    total: int | None

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_content_range(value: str | None) -> ContentRange | None:
    """Parse ``bytes 0-1023/4096``.  Returns None for malformed input."""
    if not value:
        return None
    match = _CONTENT_RANGE_RE.match(value)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    if end < start:
        return None
    if total is not None and end >= total:
        # Some broken servers clamp incorrectly; keep the facts but callers
        # should treat this as a partial response.
        return ContentRange(start, end, total)
    return ContentRange(start, end, total)


def parse_unsatisfied_range(value: str | None) -> int | None:
    """Parse the ``Content-Range: bytes */1234`` form used by 416 responses."""
    if not value:
        return None
    match = _UNSATISFIED_RANGE_RE.match(value)
    return int(match.group(1)) if match else None


def parse_retry_after(
    value: str | None, now: datetime | None = None, cap: float = 300.0
) -> float | None:
    """Return seconds to wait from a ``Retry-After`` header, or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return min(float(value), cap)
    try:
        when = parsedate_to_datetime(value)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, min((when - current).total_seconds(), cap))
    except (TypeError, ValueError, OverflowError):
        return None


def _split_header_params(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _unquote_header_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _decode_extended_filename(raw: str) -> str | None:
    """Decode RFC 5987 ``charset'lang'percent-encoded`` values."""
    first = raw.find("'")
    second = raw.find("'", first + 1) if first >= 0 else -1
    if first < 0 or second < 0:
        return None
    charset = raw[:first] or "utf-8"
    encoded = raw[second + 1 :]
    try:
        data = unquote_to_bytes(encoded)
    except Exception:
        return None
    for candidate in (charset, "utf-8", "latin-1"):
        try:
            return data.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def parse_content_disposition(value: str | None) -> str | None:
    """Return the filename from a Content-Disposition header, if any.

    ``filename*`` (RFC 5987) takes precedence over ``filename``, matching the
    behaviour of every modern browser and download manager.
    """
    if not value:
        return None
    params: dict[str, str] = {}
    for part in _split_header_params(value)[1:]:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        params[key.strip().lower()] = raw.strip()

    if "filename*" in params:
        decoded = _decode_extended_filename(
            _unquote_header_value(params["filename*"])
        )
        if decoded:
            return decoded

    if "filename" in params:
        name = _unquote_header_value(params["filename"])
        # HTTP header bytes are latin-1 on the wire.  Servers that put raw
        # UTF-8 in filename= are common in the wild; recover it when possible.
        try:
            return name.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return name
    return None


def is_strong_etag(etag: str | None) -> bool:
    return bool(etag) and not etag.startswith("W/")  # type: ignore[union-attr]


def filename_from_url(url: str) -> str | None:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    base = unquote(path.rsplit("/", 1)[-1]).strip()
    return base or None


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """Make a filename safe for Windows while keeping it recognisable."""
    name = _WINDOWS_ILLEGAL.sub("_", name).strip().rstrip(". ")
    if not name:
        return "download"

    stem, suffix = Path(name).stem, Path(name).suffix
    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    if len(suffix) > 20:  # not a real extension, treat as part of the name
        stem, suffix = name, ""

    budget = max(1, max_length - len(suffix))
    if len(stem) > budget:
        stem = stem[:budget].rstrip(". ")
    result = f"{stem}{suffix}".strip().rstrip(". ")
    return result or "download"


def unique_path(path: Path) -> Path:
    """Return ``path`` or ``name (n).ext`` if it already exists."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"could not find a free filename near {path}")


def parse_size(value: str | int | None) -> int | None:
    """Parse ``10``, ``512K``, ``1.5M``, ``2G`` into bytes."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = value.strip().lower().replace("ib", "").replace("b", "")
    if not text:
        raise ValueError("empty size")
    multiplier = 1
    if text[-1] in "kmg":
        multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3}[text[-1]]
        text = text[:-1]
    return int(float(text) * multiplier)


def parse_cookie_header(value: str | None) -> dict[str, str]:
    """Parse ``a=b; c=d`` into a dict (values may contain ``=``)."""
    if not value:
        return {}
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, val = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = val.strip()
    return cookies


def validate_header(name: str, value: str) -> None:
    """Reject CR/LF injection before user-supplied headers reach the socket."""
    if not name or any(ch in name for ch in "\r\n:"):
        raise ValueError(f"invalid header name: {name!r}")
    if "\r" in value or "\n" in value:
        raise ValueError(f"invalid value for header {name!r}")


def format_size(num: int | float | None) -> str:
    if num is None:
        return "?"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"


def format_speed(bps: float | None) -> str:
    return "?" if bps is None else f"{format_size(bps)}/s"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN check
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_duration(seconds: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(max(0.0, seconds)))
