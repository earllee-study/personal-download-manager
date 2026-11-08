"""Application settings and the small JSON files the GUI persists."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..categories import category_names, default_categories, folder_for, normalize_categories
from ..headers import parse_size
from ..models import DEFAULT_USER_AGENT
from ..paths import app_data_dir


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.exists() else Path.home()


def _clean_proxy_groups(items: Any) -> list[dict[str, str]]:
    """Ordered, de-duplicated ``[{name, proxy}]`` list from saved JSON."""
    from ..policy import normalize_proxy_groups

    cleaned: list[dict[str, str]] = []
    for name, proxy in normalize_proxy_groups(items).items():
        cleaned.append({"name": name, "proxy": proxy})
    return cleaned


def _clean_queues(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    seen = {"主队列"}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        if not name or name in seen:
            continue
        try:
            concurrent = int(item.get("concurrent") or 0)
        except (TypeError, ValueError):
            continue
        if concurrent <= 0:
            continue
        seen.add(name)
        cleaned.append({"name": name, "concurrent": max(1, min(10, concurrent))})
    return cleaned


@dataclass
class AppSettings:
    download_dir: str = field(default_factory=lambda: str(default_download_dir()))
    default_connections: int = 8
    max_concurrent: int = 3
    speed_limit: int | None = None
    # Cross-task policy: one global cap plus per-host rules.
    global_speed_limit: int | None = None
    max_host_connections: int | None = None
    host_rules: list[dict[str, Any]] = field(default_factory=list)
    # Named proxies shared by several host rules: [{"name": "us", "proxy": ...}]
    proxy_groups: list[dict[str, str]] = field(default_factory=list)
    # Extra download queues: [{"name": "夜间", "concurrent": 2}].  The main
    # queue's concurrency stays in max_concurrent.
    queues: list[dict[str, Any]] = field(default_factory=list)
    # Time-of-day speed limits: [{"start": "09:00", "end": "18:00", "limit": 1048576}]
    bandwidth_windows: list[dict[str, Any]] = field(default_factory=list)
    # Category folders: [{"name": "视频", "folder": "视频",
    # "extensions": ["mp4", ...]}]; files are saved into download_dir/folder.
    categories_enabled: bool = True
    default_category: str = "auto"   # auto | none | category name
    categories: list[dict[str, Any]] = field(default_factory=default_categories)
    auto_retry: bool = True          # retry transient failures at the queue level
    auto_retry_max: int = 3          # attempts per task, on top of engine retries
    auto_retry_delay: int = 30       # seconds before attempt 1; doubles afterwards
    # Countdown length for "shut down when all downloads finish".  The on/off
    # switch itself is a session toggle in the Download menu, deliberately not
    # persisted: starting the app must never arm a shutdown by itself.
    shutdown_delay: int = 60
    # Move finished MSE captures from the capture spool into the download
    # folder (and its category subfolder) in the background.
    mse_archive: bool = True
    # After a merged capture is archived, remove the original video/audio
    # tracks from the capture spool.  Off by default: deletion is irreversible.
    mse_delete_sources: bool = False
    clipboard_monitor: bool = False
    minimize_to_tray: bool = True
    close_to_tray: bool = True
    theme: str = "system"  # system | light | dark
    language: str = "system"  # system | zh | en
    user_agent: str = DEFAULT_USER_AGENT
    proxy: str | None = None
    verify_tls: bool = True
    # Update manifest (HTTPS). Empty means "checking is not configured"; the
    # GUI never contacts a network host unless the user clicks 检查更新.
    update_manifest: str = ""
    # Optional external ffmpeg used for DASH video+audio merging. Empty means
    # "look for ffmpeg on PATH"; it is never bundled with personal_download_manager.
    ffmpeg_path: str = ""

    def __post_init__(self) -> None:
        self.default_connections = max(1, min(64, int(self.default_connections)))
        self.max_concurrent = max(1, min(10, int(self.max_concurrent)))
        self.auto_retry_max = max(0, min(10, int(self.auto_retry_max)))
        self.auto_retry_delay = max(1, min(3600, int(self.auto_retry_delay)))
        try:
            delay = int(self.shutdown_delay)
        except (TypeError, ValueError):
            delay = 60
        self.shutdown_delay = max(1, min(3600, delay))
        self.mse_archive = bool(self.mse_archive)
        self.mse_delete_sources = bool(self.mse_delete_sources)
        if self.speed_limit is not None and self.speed_limit <= 0:
            self.speed_limit = None
        if self.global_speed_limit is not None:
            try:
                self.global_speed_limit = int(self.global_speed_limit)
            except (TypeError, ValueError):
                self.global_speed_limit = None
            if self.global_speed_limit is not None and self.global_speed_limit <= 0:
                self.global_speed_limit = None
        if self.max_host_connections is not None:
            try:
                value = int(self.max_host_connections)
            except (TypeError, ValueError):
                value = 0
            self.max_host_connections = max(1, min(64, value)) if value > 0 else None
        if not isinstance(self.host_rules, list):
            self.host_rules = []
        self.proxy_groups = _clean_proxy_groups(self.proxy_groups)
        self.queues = _clean_queues(self.queues)
        if not isinstance(self.bandwidth_windows, list):
            self.bandwidth_windows = []
        self.categories = normalize_categories(self.categories)
        valid = {"auto", "none", *category_names(self.categories)}
        if str(self.default_category) not in valid:
            self.default_category = "auto"
        if self.theme not in ("system", "light", "dark"):
            self.theme = "system"
        if str(self.language) not in ("system", "zh", "en"):
            self.language = "system"
        self.update_manifest = str(self.update_manifest or "").strip()[:2048]
        self.ffmpeg_path = str(self.ffmpeg_path or "").strip()[:2048]
        try:
            Path(self.download_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            # A removed drive or a permission problem must not stop the GUI
            # from starting; the add/settings dialogs surface the real error.
            pass

    def queue_limits(self) -> dict[str, int]:
        """Ordered {queue name: max concurrent tasks}; the main queue first."""
        limits = {"主队列": max(1, min(10, int(self.max_concurrent)))}
        for item in self.queues:
            name = str(item.get("name") or "").strip()
            if not name or name in limits:
                continue
            try:
                concurrent = int(item.get("concurrent") or 0)
            except (TypeError, ValueError):
                continue
            if concurrent > 0:
                limits[name] = max(1, min(10, concurrent))
        return limits

    def queue_names(self) -> list[str]:
        return list(self.queue_limits())

    def category_folder(self, filename: str, spec: str | None = None) -> str | None:
        """Relative category folder for ``filename`` (``None`` = root)."""
        choice = self.default_category if spec is None else spec
        return folder_for(
            filename, choice, self.categories, self.categories_enabled
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        target = path or app_data_dir() / "config.json"
        _atomic_json(target, self.to_dict())

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        target = path or app_data_dir() / "config.json"
        try:
            with open(target, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return cls.from_dict(data)
        except (OSError, ValueError, TypeError):
            pass
        return cls()


def load_downloads(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or app_data_dir() / "downloads.json"
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (OSError, ValueError):
        pass
    return []


def save_downloads(items: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or app_data_dir() / "downloads.json"
    _atomic_json(target, items)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def governor_from_settings(settings: "AppSettings"):
    """Build the shared per-host / global policy for the GUI's downloads."""
    from ..policy import Governor, HostPolicies, HostRule

    rules = []
    for item in settings.host_rules:
        if not isinstance(item, dict):
            continue
        try:
            rules.append(HostRule.from_dict(item))
        except (TypeError, ValueError):
            continue
    policies = HostPolicies(
        rules,
        default_connections=settings.max_host_connections,
        proxy_groups=settings.proxy_groups,
    )
    return Governor(policies, settings.global_speed_limit)


def parse_limit(text: str) -> int | None:
    """Parse the GUI's speed-limit field; empty/0/unlimited => None."""
    text = (text or "").strip()
    if not text or text.lower() in ("0", "unlimited", "无限", "不限"):
        return None
    value = parse_size(text)
    return value if value and value > 0 else None
