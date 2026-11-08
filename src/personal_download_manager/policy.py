"""Cross-download resource policy.

Limits apply per *server*, not per download: two tasks pulling from the same
host share one connection budget, and a "global speed limit" applies to
everything the application is doing.  This module owns that shared state.

* :class:`HostPolicies` resolves ``host:port``-less host names against
  fnmatch-style patterns (``example.com``, ``*.cdn.example.com``, ``*``) and
  yields per-host connection caps, speed limits, proxy overrides and
  site credentials (User-Agent, Cookie and Basic/Digest authentication).
* Named proxy groups let many host rules share one proxy URL
  (``proxy_groups={"us": "socks5://..."}`` + ``proxy_group="us"``), so moving
  a site to another proxy is a one-line edit instead of a table-wide change.
* :class:`ConnectionLimiter` enforces the per-host connection cap across every
  :class:`~personal_download_manager.downloader.Downloader` in the process.
* :class:`Governor` bundles the limiter with one shared token bucket used as
  the application-wide speed limit.

All objects are thread-safe; a Governor is meant to be shared by the GUI queue
or by the CLI when several downloads run at once.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .headers import parse_size
from .ratelimit import TokenBucket

log = logging.getLogger(__name__)

MAX_CONNECTIONS = 64
MAX_GROUP_NAME = 40


def host_of(value: str) -> str:
    """Accept a URL or a bare host name and return a lower-case host."""
    if "://" in value:
        return (urlsplit(value).hostname or "").lower()
    return str(value).strip().lower()


def _check_connections(value: int | None) -> int | None:
    if value is None or value == 0:
        return None
    value = int(value)
    if value < 0:
        raise ValueError("connections must be positive or None")
    return min(value, MAX_CONNECTIONS)


def _check_limit(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = parse_size(value)
    value = int(value)
    if value <= 0:
        raise ValueError("speed_limit must be positive or None")
    return value


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_auth(value: Any) -> tuple[str, str] | None:
    text = _clean_text(value)
    if text is None:
        return None
    if ":" not in text:
        raise ValueError("auth must be USER:PASSWORD")
    user, password = text.split(":", 1)
    return user, password


def normalize_proxy_groups(value: Any) -> dict[str, str]:
    """Canonical ``{name: proxy_url}`` map from JSON-friendly input.

    Accepted shapes (both used across the CLI file and the GUI settings):

        {"us": "socks5://127.0.0.1:1080"}
        [{"name": "us", "proxy": "socks5://127.0.0.1:1080"}]

    Blank names, blank proxies and malformed entries are dropped; the first
    occurrence of a name wins.
    """
    groups: dict[str, str] = {}
    if isinstance(value, Mapping):
        pairs = value.items()
    elif isinstance(value, (list, tuple)):
        pairs = []
        for item in value:
            if isinstance(item, Mapping):
                pairs.append((item.get("name"), item.get("proxy")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
    else:
        pairs = []
    for name, proxy in pairs:
        name_text = _clean_text(name)
        proxy_text = _clean_text(proxy)
        if not name_text or not proxy_text:
            continue
        key = name_text[:MAX_GROUP_NAME]
        if key in groups:  # first occurrence wins
            continue
        groups[key] = proxy_text
    return groups


@dataclass(frozen=True)
class HostRule:
    """One pattern -> policy override.  ``*`` matches every host.

    ``connections``/``speed_limit``/``proxy`` fall back to the global defaults
    when unset; ``user_agent``/``cookie``/``auth`` fall back to the task's own
    options, so a rule only overrides what it actually specifies.

    ``proxy_group`` references a named entry in ``HostPolicies.proxy_groups``.
    An explicit ``proxy`` on the rule wins over the group (see
    :meth:`HostPolicies.resolve`).
    """

    pattern: str
    connections: int | None = None
    speed_limit: int | None = None
    proxy: str | None = None
    proxy_group: str | None = None
    user_agent: str | None = None
    cookie: str | None = None
    auth: str | None = None          # "USER:PASSWORD"
    auth_type: str = "basic"         # "basic" | "digest"

    def __post_init__(self) -> None:
        if not str(self.pattern).strip():
            raise ValueError("host pattern must not be empty")
        object.__setattr__(self, "connections", _check_connections(self.connections))
        object.__setattr__(self, "speed_limit", _check_limit(self.speed_limit))
        object.__setattr__(self, "proxy", _clean_text(self.proxy))
        group = _clean_text(self.proxy_group)
        object.__setattr__(
            self, "proxy_group", group[:MAX_GROUP_NAME] if group else None
        )
        object.__setattr__(self, "user_agent", _clean_text(self.user_agent))
        object.__setattr__(self, "cookie", _clean_text(self.cookie))
        object.__setattr__(self, "auth", _clean_text(self.auth))
        if self.auth is not None:
            _parse_auth(self.auth)  # validation only
        auth_type = str(self.auth_type or "basic").strip().lower()
        if auth_type not in ("basic", "digest"):
            raise ValueError("auth_type must be 'basic' or 'digest'")
        object.__setattr__(self, "auth_type", auth_type)

    def matches(self, host: str) -> bool:
        return fnmatch.fnmatchcase(host, str(self.pattern).strip().lower())

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "connections": self.connections,
            "speed_limit": self.speed_limit,
            "proxy": self.proxy,
            "proxy_group": self.proxy_group,
            "user_agent": self.user_agent,
            "cookie": self.cookie,
            "auth": self.auth,
            "auth_type": self.auth_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HostRule":
        return cls(
            pattern=str(data.get("pattern", "")),
            connections=data.get("connections"),
            speed_limit=data.get("speed_limit"),
            proxy=data.get("proxy") or None,
            proxy_group=data.get("proxy_group") or None,
            user_agent=data.get("user_agent") or None,
            cookie=data.get("cookie") or None,
            auth=data.get("auth") or None,
            auth_type=data.get("auth_type") or "basic",
        )


@dataclass(frozen=True)
class ResolvedPolicy:
    connections: int | None = None
    speed_limit: int | None = None
    proxy: str | None = None
    user_agent: str | None = None
    cookie: str | None = None
    auth: tuple[str, str] | None = None
    auth_type: str = "basic"


class HostPolicies:
    def __init__(
        self,
        rules: Iterable[HostRule] = (),
        *,
        default_connections: int | None = None,
        default_speed_limit: int | None = None,
        default_proxy: str | None = None,
        proxy_groups: Any = None,
    ) -> None:
        self.rules = tuple(rules)
        self.default_connections = _check_connections(default_connections)
        self.default_speed_limit = _check_limit(default_speed_limit)
        self.default_proxy = default_proxy or None
        self.proxy_groups = normalize_proxy_groups(proxy_groups)
        unknown = sorted(self.unknown_groups())
        if unknown:
            log.warning(
                "host rules reference unknown proxy groups (falling back to the "
                "default proxy): %s",
                ", ".join(unknown),
            )

    def unknown_groups(self) -> list[str]:
        """Names referenced by rules but absent from ``proxy_groups``."""
        return [
            rule.proxy_group
            for rule in self.rules
            if rule.proxy_group and rule.proxy_group not in self.proxy_groups
        ]

    def _proxy_for(self, rule: HostRule) -> str | None:
        # Precedence: explicit rule proxy > named group > global default.
        if rule.proxy is not None:
            return rule.proxy
        if rule.proxy_group:
            group_proxy = self.proxy_groups.get(rule.proxy_group)
            if group_proxy is not None:
                return group_proxy
        return self.default_proxy

    def resolve(self, value: str) -> ResolvedPolicy:
        host = host_of(value)
        for rule in self.rules:
            if rule.matches(host):
                return ResolvedPolicy(
                    connections=(
                        rule.connections
                        if rule.connections is not None
                        else self.default_connections
                    ),
                    speed_limit=(
                        rule.speed_limit
                        if rule.speed_limit is not None
                        else self.default_speed_limit
                    ),
                    proxy=self._proxy_for(rule),
                    user_agent=rule.user_agent,
                    cookie=rule.cookie,
                    auth=_parse_auth(rule.auth),
                    auth_type=rule.auth_type,
                )
        return ResolvedPolicy(
            connections=self.default_connections,
            speed_limit=self.default_speed_limit,
            proxy=self.default_proxy,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaults": {
                "connections": self.default_connections,
                "speed_limit": self.default_speed_limit,
                "proxy": self.default_proxy,
            },
            "proxy_groups": dict(self.proxy_groups),
            "hosts": [rule.to_dict() for rule in self.rules],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HostPolicies":
        if not isinstance(data, dict):
            return cls()
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, dict):
            defaults = {}
        raw_rules = data.get("hosts") or data.get("rules") or []
        rules = []
        for item in raw_rules if isinstance(raw_rules, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                rules.append(HostRule.from_dict(item))
            except (TypeError, ValueError):
                continue
        return cls(
            rules,
            default_connections=defaults.get("connections"),
            default_speed_limit=defaults.get("speed_limit"),
            default_proxy=defaults.get("proxy"),
            proxy_groups=data.get("proxy_groups"),
        )


class ConnectionLimiter:
    """Process-wide per-host connection budget."""

    def __init__(self, policies: HostPolicies) -> None:
        self._policies = policies
        self._condition = threading.Condition()
        self._active: dict[str, int] = {}

    def acquire(self, value: str, cancel_event: threading.Event | None = None) -> bool:
        """Take one connection slot; returns False if cancelled while waiting."""
        host = host_of(value)
        cap = self._policies.resolve(host).connections
        with self._condition:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return False
                active = self._active.get(host, 0)
                if cap is None or active < cap:
                    self._active[host] = active + 1
                    return True
                self._condition.wait(0.05)

    def release(self, value: str) -> None:
        host = host_of(value)
        with self._condition:
            active = self._active.get(host, 0)
            if active > 0:
                self._active[host] = active - 1
            self._condition.notify_all()

    def active(self, value: str) -> int:
        with self._condition:
            return self._active.get(host_of(value), 0)


class Governor:
    """Shared connection budget + application-wide speed limit."""

    def __init__(
        self,
        policies: HostPolicies | None = None,
        global_speed_limit: int | None = None,
    ) -> None:
        self.policies = policies or HostPolicies()
        self.limiter = ConnectionLimiter(self.policies)
        self._bucket = TokenBucket(_check_limit(global_speed_limit))

    @property
    def global_speed_limit(self) -> int | None:
        rate = int(self._bucket.rate)
        return rate or None

    def set_global_speed_limit(self, limit: int | None) -> None:
        self._bucket.set_rate(_check_limit(limit))

    def resolve(self, value: str) -> ResolvedPolicy:
        return self.policies.resolve(value)

    def acquire(self, value: str, cancel_event: threading.Event | None = None) -> bool:
        return self.limiter.acquire(value, cancel_event)

    def release(self, value: str) -> None:
        self.limiter.release(value)

    def consume(self, count: int) -> None:
        self._bucket.consume(count)


_default_lock = threading.Lock()
_default_governor: Governor | None = None


def default_governor() -> Governor:
    """Process-wide fallback used when a DownloadConfig carries no governor."""
    global _default_governor
    with _default_lock:
        if _default_governor is None:
            _default_governor = Governor()
        return _default_governor


def set_default_governor(governor: Governor | None) -> None:
    global _default_governor
    with _default_lock:
        _default_governor = governor
