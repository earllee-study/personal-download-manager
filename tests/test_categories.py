"""Category folders: extension matching, choices and sanitising."""

from __future__ import annotations

from pathlib import Path

from personal_download_manager.categories import (
    Category,
    category_for,
    clean_folder,
    default_categories,
    folder_for,
    normalize_categories,
    target_dir,
)


def test_default_categories_classify_common_extensions():
    categories = default_categories()
    cases = {
        "song.mp3": "音乐",
        "movie.MKV": "视频",
        "setup.exe": "程序",
        "report.pdf": "文档",
        "archive.7z": "压缩文件",
    }
    for filename, expected in cases.items():
        folder = folder_for(filename, "auto", categories)
        assert folder == expected, filename


def test_unknown_or_missing_extension_stays_in_the_root():
    categories = default_categories()
    assert category_for("file.unknownext", categories) is None
    assert folder_for("file.unknownext", "auto", categories) is None
    assert folder_for("README", "auto", categories) is None
    assert folder_for("", "auto", categories) is None


def test_explicit_choice_none_auto_and_name():
    categories = default_categories()
    assert folder_for("movie.mp4", "none", categories) is None
    assert folder_for("movie.mp4", "", categories) is None
    assert folder_for("movie.mp4", "视频", categories) == "视频"
    assert folder_for("movie.mp4", "文档", categories) == "文档"
    # An unknown category name must not invent a folder.
    assert folder_for("movie.mp4", "不存在", categories) is None


def test_disabled_categories_never_move_files():
    categories = default_categories()
    assert folder_for("movie.mp4", "auto", categories, enabled=False) is None
    assert folder_for("movie.mp4", "视频", categories, enabled=False) is None


def test_target_dir_joins_relative_folders():
    base = Path("D:/downloads")
    categories = default_categories()
    assert target_dir(base, "a.mp4", "auto", categories) == base / "视频"
    assert target_dir(base, "a.bin", "auto", categories) == base


def test_folder_sanitising_cannot_escape_the_download_root():
    categories = normalize_categories(
        [{"name": "危险", "folder": "../../etc", "extensions": "evil"}]
    )
    folder = folder_for("x.evil", "自动", [])  # unknown choice -> None
    assert folder is None
    resolved = folder_for("x.evil", "危险", categories)
    assert resolved is not None
    assert ".." not in resolved
    assert not Path(resolved).is_absolute()


def test_clean_folder_handles_separators_and_empty_parts():
    assert clean_folder("视频") == "视频"
    assert clean_folder(" a / b ") == "a/b"
    assert clean_folder("..\\..\\x") == "x"
    assert clean_folder("   ") == ""


def test_custom_table_is_normalised_and_deduplicated():
    rows = [
        {"name": "音频", "folder": "Audio", "extensions": ["MP3", "*.flac", ".mp3"]},
        {"name": "音频", "folder": "Dup", "extensions": ["wav"]},  # duplicate name
        {"name": "", "folder": "x", "extensions": ["x"]},          # no name
        {"name": "视频", "folder": "", "extensions": "mp4,mp3"},    # mp3 taken
    ]
    cleaned = normalize_categories(rows)
    assert cleaned == [
        {"name": "音频", "folder": "Audio", "extensions": ["mp3", "flac"]},
        {"name": "视频", "folder": "视频", "extensions": ["mp4"]},
    ]
    assert category_for("a.mp3", cleaned)["name"] == "音频"
    assert category_for("a.flac", cleaned)["name"] == "音频"
    assert category_for("a.mp4", cleaned)["name"] == "视频"


def test_normalize_falls_back_to_defaults_for_non_lists():
    assert normalize_categories(None) == default_categories()
    assert normalize_categories("nonsense") == default_categories()
    assert normalize_categories([]) == []


def test_category_dict_roundtrip():
    category = Category("视频", "视频", ("mp4", "mkv"))
    restored = Category.from_dict(category.to_dict())
    assert restored == category

    fallback = Category.from_dict({"name": "只看名字"})
    assert fallback.folder == "只看名字"
