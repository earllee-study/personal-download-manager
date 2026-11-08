"""Wildcard batch URL expansion ("URL 通配" batch download).

One pattern per line, with three token kinds:

* ``[1-10]`` / ``[001-020]`` - numeric range, zero padding preserved and an
  optional ``:step`` suffix (``[1-10:2]`` -> 1,3,5,7,9);
* ``[a-z]`` / ``[A-Z]`` - single-character range, optional ``:step``;
* ``{a,b,c}`` - alternatives.

Example::

    https://cdn.example.com/img[01-03].jpg
    https://mirror.example.com/{a,b}/file.bin

expands to five URLs, in the order written.  Expansion is deterministic and
bounded (``limit``) so a typo such as ``[1-999999]`` fails loudly instead of
queueing a million tasks.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence
from urllib.parse import urlsplit

DEFAULT_LIMIT = 10_000

_TOKEN_RE = re.compile(
    r"\[(?:(?P<nstart>\d+)-(?P<nend>\d+)(?::(?P<nstep>\d+))?"
    r"|(?P<cstart>[A-Za-z])-(?P<cend>[A-Za-z])(?::(?P<cstep>\d+))?)\]"
    r"|\{(?P<alts>[^{}]*)\}"
)


class UrlBatchError(ValueError):
    """A pattern is malformed or expands past the safety limit."""


def _numeric_values(start: int, end: int, step: int, text: str) -> list[str]:
    if step < 1:
        raise UrlBatchError(f"invalid step in {text!r}")
    if start > end:
        raise UrlBatchError(f"descending range is not supported: {text!r}")
    width = len(text) if text.startswith("0") and len(text) > 1 else 0
    return [str(value).zfill(width) for value in range(start, end + 1, step)]


def _char_values(start: str, end: str, step: int, text: str) -> list[str]:
    if step < 1:
        raise UrlBatchError(f"invalid step in {text!r}")
    if not (start.islower() == end.islower()):
        raise UrlBatchError(f"range must not mix letter cases: {text!r}")
    first, last = ord(start), ord(end)
    if first > last:
        raise UrlBatchError(f"descending range is not supported: {text!r}")
    return [chr(code) for code in range(first, last + 1, step)]


def _expand_once(pattern: str) -> list[str] | None:
    """Expand the first token, or return ``None`` when there is none."""
    match = _TOKEN_RE.search(pattern)
    if match is None:
        return None
    token = match.group(0)
    if match.group("alts") is not None:
        values = match.group("alts").split(",")
        if not values or all(not value.strip() for value in values):
            raise UrlBatchError(f"empty alternatives in {token!r}")
    elif match.group("nstart") is not None:
        values = _numeric_values(
            int(match.group("nstart")),
            int(match.group("nend")),
            int(match.group("nstep") or 1),
            match.group("nstart"),
        )
    else:
        values = _char_values(
            match.group("cstart"),
            match.group("cend"),
            int(match.group("cstep") or 1),
            match.group("cstart"),
        )
    prefix, suffix = pattern[: match.start()], pattern[match.end() :]
    return [prefix + value + suffix for value in values]


def expand_pattern(pattern: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Expand a single pattern into concrete URLs (order preserved)."""
    text = str(pattern or "").strip()
    if not text:
        return []
    urls = [text]
    while True:
        expanded: list[str] = []
        changed = False
        for url in urls:
            parts = _expand_once(url)
            if parts is None:
                expanded.append(url)
            else:
                changed = True
                expanded.extend(parts)
            if len(expanded) > limit:
                raise UrlBatchError(
                    f"pattern expands to more than {limit} URLs; "
                    "narrow the range or raise the limit"
                )
        urls = expanded
        if not changed:
            return urls


def expand_url_patterns(
    patterns: str | Iterable[str],
    limit: int = DEFAULT_LIMIT,
) -> list[str]:
    """Expand lines (or a list of patterns) into a de-duplicated URL list.

    Blank lines and ``#`` comments are ignored.  Duplicates keep their first
    position so a pattern list never queues the same URL twice.
    """
    if isinstance(patterns, str):
        lines: Sequence[str] = patterns.splitlines()
    else:
        lines = list(patterns)
    if limit < 1:
        raise UrlBatchError("limit must be positive")

    urls: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = str(line).strip()
        if not text or text.startswith("#"):
            continue
        for url in expand_pattern(text, limit):
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) > limit:
                raise UrlBatchError(
                    f"batch expands to more than {limit} URLs; "
                    "narrow the patterns"
                )
    return urls


# Schemes the engine can download.  Browser capture remains http(s)-only.
SUPPORTED_SCHEMES = frozenset({"http", "https", "ftp", "ftps", "sftp"})
HTTP_SCHEMES = frozenset({"http", "https"})


def is_http_url(text: str) -> bool:
    """Whether HLS/DASH/HTTP-only features apply to this URL."""
    try:
        return urlsplit(str(text).strip()).scheme.lower() in HTTP_SCHEMES
    except ValueError:
        return False


def looks_like_url(text: str) -> bool:
    """Cheap supported-scheme check used by batch dialogs before accepting."""
    try:
        parts = urlsplit(str(text).strip())
    except ValueError:
        return False
    return parts.scheme.lower() in SUPPORTED_SCHEMES and bool(parts.netloc)
