"""Recurring download plans (daily / weekly / every N days).

A planned start can repeat: when a task finishes -- successfully or after its
retries are exhausted -- the queue re-arms it for the next matching local
wall-clock time instead of leaving it completed/failed.

The rule travels with the task as plain JSON, so it survives a restart:

    {"kind": "daily",  "time": "03:30"}
    {"kind": "weekly", "time": "22:00", "weekdays": [0, 4]}
    {"kind": "interval", "time": "03:30", "days": 3, "anchor": "2026-01-01"}

``weekdays`` uses Python's convention (Monday == 0) and only applies to
``weekly``.  ``interval`` repeats every N days: occurrences fall on
``anchor + k * days`` (the anchor defaults to the local date when the rule is
first parsed, and is persisted so restarts keep the same cadence).  Everything
here is a pure function of the local clock, which keeps the scheduler testable
without waiting for a real day to pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from .bandwidth import parse_hhmm

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAY_SHORT = ("一", "二", "三", "四", "五", "六", "日")
KINDS = ("daily", "weekly", "interval")
MAX_INTERVAL_DAYS = 3650


def _clean_interval_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("interval repeat needs days between 1 and 3650") from exc
    if not 1 <= days <= MAX_INTERVAL_DAYS:
        raise ValueError("interval repeat needs days between 1 and 3650")
    return days


def _clean_anchor(value: Any) -> str:
    """Normalise a persisted anchor date; empty means "today"."""
    if value is None or value == "":
        return date.today().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError("interval anchor must be a YYYY-MM-DD date") from exc


def _clean_weekdays(value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if isinstance(value, (int, str)):
        value = [value]
    days: set[int] = set()
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError("weekdays must be integers 0-6") from exc
        if not 0 <= day <= 6:
            raise ValueError("weekdays must be integers 0-6")
        days.add(day)
    return tuple(sorted(days))


@dataclass(frozen=True)
class RepeatRule:
    kind: str
    hour: int
    minute: int
    weekdays: tuple[int, ...] = ()
    days: int = 0
    anchor: str = ""

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        if kind not in KINDS:
            raise ValueError(
                "repeat kind must be 'daily', 'weekly' or 'interval'"
            )
        object.__setattr__(self, "kind", kind)
        try:
            hour = int(self.hour)
            minute = int(self.minute)
        except (TypeError, ValueError) as exc:
            raise ValueError("repeat time must be HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("repeat time must be HH:MM")
        object.__setattr__(self, "hour", hour)
        object.__setattr__(self, "minute", minute)
        weekdays = _clean_weekdays(self.weekdays)
        if kind == "weekly" and not weekdays:
            raise ValueError("weekly repeat needs at least one weekday")
        if kind != "weekly":
            weekdays = ()
        object.__setattr__(self, "weekdays", weekdays)
        if kind == "interval":
            object.__setattr__(self, "days", _clean_interval_days(self.days))
            object.__setattr__(self, "anchor", _clean_anchor(self.anchor))
        else:
            object.__setattr__(self, "days", 0)
            object.__setattr__(self, "anchor", "")

    @property
    def time_text(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    def describe(self) -> str:
        if self.kind == "daily":
            return f"每天 {self.time_text}"
        if self.kind == "interval":
            if self.days == 1:
                return f"每天 {self.time_text}"
            return f"每隔 {self.days} 天 {self.time_text}"
        names = "、".join(WEEKDAY_LABELS[day] for day in self.weekdays)
        return f"每周 {names} {self.time_text}"

    def next_after(self, now: float | None = None) -> float:
        """Epoch seconds of the first matching local time strictly after now."""
        current = datetime.fromtimestamp(now) if now is not None else datetime.now()
        moment = time(self.hour, self.minute)
        if self.kind == "interval":
            anchor = date.fromisoformat(self.anchor)
            start = max(current.date(), anchor)
            # First occurrence on/after ``start`` is anchor + k * days; the
            # modulo picks that k without looping over every day.
            offset = (-(start - anchor).days) % self.days
            candidate = datetime.combine(start + timedelta(days=offset), moment)
            if candidate <= current:
                candidate += timedelta(days=self.days)
            return candidate.timestamp()
        for offset in range(0, 8):
            day = current.date() + timedelta(days=offset)
            if self.kind == "weekly" and day.weekday() not in self.weekdays:
                continue
            candidate = datetime.combine(day, moment)
            if candidate > current:
                return candidate.timestamp()
        # Unreachable for a valid rule (daily matches tomorrow, weekly within
        # a week), but never leave a caller without a time.
        return (current + timedelta(days=1)).timestamp()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind, "time": self.time_text}
        if self.kind == "weekly":
            payload["weekdays"] = list(self.weekdays)
        elif self.kind == "interval":
            payload["days"] = self.days
            payload["anchor"] = self.anchor
        return payload


def parse_repeat(value: Any) -> RepeatRule | None:
    """Parse a task's ``repeat`` value; ``None``/empty means no recurrence."""
    if value is None or value == "":
        return None
    if isinstance(value, RepeatRule):
        return value
    if not isinstance(value, dict):
        raise ValueError("repeat must be an object")
    text = str(value.get("time") or "").strip()
    minutes = parse_hhmm(text)
    if minutes is None:
        raise ValueError("repeat time must be HH:MM")
    return RepeatRule(
        kind=str(value.get("kind") or "daily"),
        hour=minutes // 60,
        minute=minutes % 60,
        weekdays=_clean_weekdays(value.get("weekdays")),
        days=value.get("days", 0),
        anchor=value.get("anchor") or "",
    )


def repeat_label(value: Any) -> str:
    """Human label for a stored repeat value; empty string when invalid/none."""
    try:
        rule = parse_repeat(value)
    except ValueError:
        return ""
    return rule.describe() if rule is not None else ""
