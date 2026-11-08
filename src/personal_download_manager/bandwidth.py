"""Time-of-day bandwidth windows.

A window like ``22:00-06:00 @ 2M`` means "between 22:00 and 06:00 the global
speed limit is 2 MiB/s"; windows may wrap midnight.  Outside every window the
configured global limit applies.  When several windows overlap, the strictest
(smallest) limit wins; a window with ``limit = None`` means "unlimited" but
only when no other active window sets a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .headers import parse_size


@dataclass(frozen=True)
class BandwidthWindow:
    start: int  # minutes since midnight
    end: int
    limit: int | None = None


def parse_hhmm(value: Any) -> int | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    hours, _, minutes = text.partition(":")
    try:
        hour = int(hours)
        minute = int(minutes)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def parse_window(item: Any) -> BandwidthWindow | None:
    if not isinstance(item, dict):
        return None
    start = parse_hhmm(item.get("start"))
    end = parse_hhmm(item.get("end"))
    if start is None or end is None or start == end:
        return None
    limit = item.get("limit")
    if isinstance(limit, str):
        limit = parse_size(limit)
    try:
        limit = int(limit) if limit else None
    except (TypeError, ValueError):
        limit = None
    if limit is not None and limit <= 0:
        limit = None
    return BandwidthWindow(start=start, end=end, limit=limit)


def parse_windows(items: Iterable[Any] | None) -> list[BandwidthWindow]:
    windows: list[BandwidthWindow] = []
    for item in items or []:
        window = parse_window(item)
        if window is not None:
            windows.append(window)
    return windows


def limit_at(
    moment: datetime,
    windows: list[BandwidthWindow],
    default: int | None,
) -> int | None:
    minutes = moment.hour * 60 + moment.minute
    active: list[BandwidthWindow] = []
    for window in windows:
        if window.start < window.end:
            inside = window.start <= minutes < window.end
        else:  # wraps midnight
            inside = minutes >= window.start or minutes < window.end
        if inside:
            active.append(window)
    if not active:
        return default
    limits = [window.limit for window in active if window.limit is not None]
    return min(limits) if limits else None
