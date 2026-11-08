"""Download categories by file type (子目录分类).

Finished downloads are sorted into per-type folders (Music, Video, Programs,
Documents, Compressed, ...).  The same idea is implemented here as a small,
side-effect-free module shared by the GUI, the CLI and the browser capture
path:

* :data:`DEFAULT_CATEGORIES` is the ordered default table.  The first category
  whose extension list contains the file's suffix wins; unknown suffixes fall
  back to the base download directory (there is no forced "Other" folder).
* :func:`folder_for` resolves a user choice (``"auto"``, ``"none"`` or an
  explicit category name) to a relative folder, or ``None`` for the base
  directory.
* :func:`target_dir` joins that folder to the base directory.
* :func:`normalize_categories` validates settings loaded from JSON.

Folders are relative to the download directory and are sanitised segment by
segment, so a hand-edited config cannot escape the download root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .headers import sanitize_filename

AUTO = "auto"
NONE = "none"

_EXT_RE = re.compile(r"^[a-z0-9]{1,16}$")


@dataclass(frozen=True)
class Category:
    """One category: a display name, a relative folder and its extensions."""

    name: str
    folder: str
    extensions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "folder": self.folder,
            "extensions": list(self.extensions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Category":
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("category name must not be empty")
        return cls(
            name=name,
            folder=clean_folder(data.get("folder", name)) or clean_folder(name),
            extensions=parse_extensions(data.get("extensions")),
        )


DEFAULT_CATEGORIES: tuple[Category, ...] = (
    Category(
        "音乐",
        "音乐",
        ("mp3", "flac", "wav", "m4a", "aac", "ogg", "wma", "opus", "ape"),
    ),
    Category(
        "视频",
        "视频",
        ("mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v",
         "mpg", "mpeg", "ts", "rmvb", "3gp"),
    ),
    Category(
        "程序",
        "程序",
        ("exe", "msi", "msix", "apk", "appx", "appxbundle",
         "dmg", "pkg", "deb", "rpm"),
    ),
    Category(
        "文档",
        "文档",
        ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
         "epub", "mobi", "odt", "ods", "rtf", "csv", "md"),
    ),
    Category(
        "压缩文件",
        "压缩文件",
        ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz",
         "iso", "cab", "zst"),
    ),
)


def default_categories() -> list[dict[str, Any]]:
    return [category.to_dict() for category in DEFAULT_CATEGORIES]


def clean_folder(value: Any, fallback: str = "") -> str:
    """Sanitise a relative folder path; ``""`` means "download root"."""
    text = str(value if value is not None else "").strip().replace("\\", "/")
    parts = []
    for part in text.split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        parts.append(sanitize_filename(part))
    return "/".join(parts) if parts else clean_folder(fallback) if fallback else ""


def parse_extensions(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items: Iterable[Any] = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        ext = str(item).strip().lower().lstrip("*").lstrip(".")
        if not ext or not _EXT_RE.match(ext) or ext in seen:
            continue
        seen.add(ext)
        cleaned.append(ext)
    return tuple(cleaned)


def normalize_categories(items: Any) -> list[dict[str, Any]]:
    """Validate a category table from settings; invalid rows are dropped.

    Names are unique (case-insensitive) and an extension belongs to the first
    category that claims it, which keeps ``category_for`` deterministic.
    """
    if isinstance(items, tuple):
        items = list(items)
    if not isinstance(items, list):
        return default_categories()

    cleaned: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_exts: set[str] = set()
    for item in items:
        if isinstance(item, Category):
            item = item.to_dict()
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        if not name or name.lower() in seen_names:
            continue
        folder = clean_folder(item.get("folder", name), fallback=name)
        extensions = []
        for ext in parse_extensions(item.get("extensions")):
            if ext in seen_exts:
                continue
            seen_exts.add(ext)
            extensions.append(ext)
        seen_names.add(name.lower())
        cleaned.append(
            {"name": name, "folder": folder or name, "extensions": extensions}
        )
    return cleaned


def category_for(filename: str, categories: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first category claiming ``filename``'s extension."""
    suffix = Path(str(filename or "")).suffix.lower().lstrip(".")
    if not suffix:
        return None
    for category in categories:
        if suffix in (category.get("extensions") or ()):
            return category
    return None


def folder_for(
    filename: str,
    spec: str | None,
    categories: Iterable[dict[str, Any]],
    enabled: bool = True,
) -> str | None:
    """Resolve a category choice to a relative folder (``None`` = root).

    ``spec`` is ``"auto"`` (by extension), ``"none"``/empty (no subfolder) or
    an explicit category name.  Unknown names and unmatched extensions fall
    back to the download root instead of inventing a folder.
    """
    if not enabled:
        return None
    choice = str(spec or NONE).strip()
    categories = list(categories)
    if choice.lower() in ("", NONE, "off", "不分分类", "不分类", "关闭"):
        return None
    if choice.lower() == AUTO:
        category = category_for(filename, categories)
        return clean_folder(category.get("folder")) if category else None
    for category in categories:
        if str(category.get("name")) == choice:
            return clean_folder(category.get("folder")) or None
    return None


def target_dir(
    base: str | Path,
    filename: str,
    spec: str | None,
    categories: Iterable[dict[str, Any]],
    enabled: bool = True,
) -> Path:
    """``base`` plus the category folder (if any)."""
    root = Path(base)
    folder = folder_for(filename, spec, categories, enabled)
    if not folder:
        return root
    return root.joinpath(*folder.split("/"))


def category_names(categories: Iterable[dict[str, Any]]) -> list[str]:
    return [str(item.get("name")) for item in categories if item.get("name")]
