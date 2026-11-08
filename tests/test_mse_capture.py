"""Finished MSE captures become normal (completed) GUI queue entries."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from personal_download_manager.gui import i18n
from personal_download_manager.gui.capture import (
    load_mse_captures,
    make_mse_task,
)
from personal_download_manager.gui.manager import QueueManager
from personal_download_manager.gui.model import (
    STATUS_COMPLETED,
    DownloadTableModel,
)
from personal_download_manager.gui.settings import AppSettings


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def reset_language():
    i18n.set_language("zh")
    yield
    i18n.set_language("zh")


def settle(delay: float = 0.05) -> None:
    QApplication.processEvents()
    time.sleep(delay)
    QApplication.processEvents()


def mse_dir(tmp_path: Path) -> Path:
    root = tmp_path / "capture" / "mse"
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_file(directory: Path, name: str, data: bytes) -> Path:
    path = directory / name
    path.write_bytes(data)
    return path


def track(path: Path, kind: str, mime: str, name: str) -> dict:
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": "a" * 64,
        "mime": mime,
        "kind": kind,
        "track": name,
    }


def write_manifest(
    tmp_path: Path,
    *,
    session: str = "s1",
    files: list[dict] | None = None,
    merged: dict | None = None,
    source_url: str | None = "https://example.com/watch?v=1",
    title: str | None = "Demo video",
    merge_error: str | None = None,
) -> Path:
    path = mse_dir(tmp_path) / f"mse-session-{session}.json"
    payload = {
        "kind": "mse",
        "version": 1,
        "session": session,
        "created_at": 1790000000.0,
        "source_url": source_url,
        "title": title,
        "files": files or [],
        "merged": merged,
        "merge_error": merge_error,
        "errors": [],
        "track_count": len(files or []),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# manifest parsing
# ----------------------------------------------------------------------
def test_merged_capture_becomes_a_completed_task(tmp_path):
    root = mse_dir(tmp_path)
    video = make_file(root, "clip.mp4", b"VIDEO")
    audio = make_file(root, "clip.m4a", b"AUDIO")
    merged = make_file(root, "clip.merged.mp4", b"MERGED!")
    manifest = write_manifest(
        tmp_path,
        files=[track(video, "video", "video/mp4", "v"),
               track(audio, "audio", "audio/mp4", "a")],
        merged=track(merged, "video", "video/mp4", ""),
    )

    captures = load_mse_captures(tmp_path / "capture")
    assert len(captures) == 1
    path, capture = captures[0]
    assert path == manifest
    assert capture.session == "s1"
    assert capture.source_url == "https://example.com/watch?v=1"
    assert capture.title == "Demo video"

    task = make_mse_task(capture)
    assert task.status == STATUS_COMPLETED
    assert task.status_label == "已捕获"
    assert task.is_capture
    assert task.output == str(merged)
    assert task.url == "https://example.com/watch?v=1"
    assert task.total_size == task.downloaded == len(b"MERGED!")
    assert task.completed_at == 1790000000.0
    assert task.options["capture"] == "mse"
    assert task.options["mse_session"] == "s1"
    assert task.options["mse_merged"] is True
    assert set(task.options["extra_paths"]) == {str(video), str(audio)}
    assert "MSE 捕获（2 个轨道）" in DownloadTableModel._status_tooltip(task)

    i18n.set_language("en")
    assert task.status_label == "Captured"
    i18n.set_language("zh")


def test_unmerged_capture_prefers_the_largest_video(tmp_path):
    root = mse_dir(tmp_path)
    video = make_file(root, "small-video.mp4", b"V" * 4)
    audio = make_file(root, "big-audio.m4a", b"A" * 100)
    write_manifest(
        tmp_path,
        files=[track(video, "video", "video/mp4", "v"),
               track(audio, "audio", "audio/mp4", "a")],
        merge_error="ffmpeg not found",
    )
    capture = load_mse_captures(tmp_path / "capture")[0][1]
    task = make_mse_task(capture)
    assert task.output == str(video)
    assert task.options["mse_merged"] is False
    assert task.options["extra_paths"] == [str(audio)]
    assert "未合并：ffmpeg not found" in DownloadTableModel._status_tooltip(task)


def test_manifest_paths_outside_the_spool_are_rejected(tmp_path):
    root = mse_dir(tmp_path)
    outside = make_file(tmp_path, "evil.mp4", b"NOT A CAPTURE")
    manifest = write_manifest(
        tmp_path, files=[track(outside, "video", "video/mp4", "v")]
    )
    assert load_mse_captures(tmp_path / "capture") == []
    assert not manifest.exists(), "malformed manifests are removed"


def test_mixed_manifest_keeps_only_safe_files(tmp_path):
    root = mse_dir(tmp_path)
    good = make_file(root, "good.mp4", b"GOOD")
    outside = make_file(tmp_path, "outside.mp4", b"NO")
    write_manifest(
        tmp_path,
        files=[track(good, "video", "video/mp4", "v"),
               track(outside, "audio", "audio/mp4", "a")],
    )
    capture = load_mse_captures(tmp_path / "capture")[0][1]
    assert [item["path"] for item in capture.files] == [str(good)]
    task = make_mse_task(capture)
    assert task.output == str(good)
    assert str(outside) not in task.options["extra_paths"]


def test_missing_capture_file_is_dropped(tmp_path):
    manifest = write_manifest(
        tmp_path,
        files=[{"path": str(mse_dir(tmp_path) / "missing.mp4"),
                "kind": "video", "mime": "video/mp4"}],
    )
    assert load_mse_captures(tmp_path / "capture") == []
    assert not manifest.exists()


# ----------------------------------------------------------------------
# queue manager / main window integration
# ----------------------------------------------------------------------
def test_manager_adds_captured_task_without_starting_it(qapp, tmp_path):
    root = mse_dir(tmp_path)
    merged = make_file(root, "clip.merged.mp4", b"MERGED")
    write_manifest(
        tmp_path,
        files=[track(merged, "video", "video/mp4", "v")],
        merged=track(merged, "video", "video/mp4", ""),
    )
    task = make_mse_task(load_mse_captures(tmp_path / "capture")[0][1])
    settings = AppSettings(download_dir=str(tmp_path))
    manager = QueueManager(DownloadTableModel(), settings)
    try:
        manager.add_captured_task(task)
        assert manager.workers == {}
        assert manager.model.find(task.id).status == STATUS_COMPLETED
        assert manager._queue == []
    finally:
        manager.shutdown()
        settle()


def test_deleting_a_capture_task_removes_all_tracks(qapp, tmp_path):
    root = mse_dir(tmp_path)
    video = make_file(root, "clip.mp4", b"VIDEO")
    audio = make_file(root, "clip.m4a", b"AUDIO")
    merged = make_file(root, "clip.merged.mp4", b"MERGED")
    write_manifest(
        tmp_path,
        files=[track(video, "video", "video/mp4", "v"),
               track(audio, "audio", "audio/mp4", "a")],
        merged=track(merged, "video", "video/mp4", ""),
    )
    task = make_mse_task(load_mse_captures(tmp_path / "capture")[0][1])
    settings = AppSettings(download_dir=str(tmp_path))
    manager = QueueManager(DownloadTableModel(), settings)
    try:
        manager.add_captured_task(task)
        manager.remove(task.id, delete_files=True)
        assert manager.model.find(task.id) is None
        assert not merged.exists()
        assert not video.exists()
        assert not audio.exists()
    finally:
        manager.shutdown()
        settle()


def test_main_window_imports_each_mse_session_once(
    qapp, tmp_path, monkeypatch
):
    import personal_download_manager.gui.main_window as main_window

    spool = tmp_path / "capture"
    monkeypatch.setattr(main_window, "capture_dir", lambda: spool)
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)

    settings = AppSettings(download_dir=str(tmp_path), mse_archive=False)
    window = main_window.MainWindow(settings)
    try:
        root = mse_dir(tmp_path)
        merged = make_file(root, "window.merged.mp4", b"WINDOW")
        manifest = write_manifest(
            tmp_path,
            session="window-1",
            files=[track(merged, "video", "video/mp4", "v")],
            merged=track(merged, "video", "video/mp4", ""),
            title="Window capture",
        )
        window._poll_captures()

        tasks = window.model.tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status == STATUS_COMPLETED
        assert task.is_capture and task.output == str(merged)
        assert task.options["mse_session"] == "window-1"
        assert not manifest.exists()

        # The manifest reappears (e.g. GUI restarted before it saved state):
        # the session id must prevent a duplicate row.
        write_manifest(
            tmp_path,
            session="window-1",
            files=[track(merged, "video", "video/mp4", "v")],
            merged=track(merged, "video", "video/mp4", ""),
        )
        window._poll_captures()
        assert len(window.model.tasks()) == 1
    finally:
        window.manager.shutdown()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window._really_quit = True
        window.close()
        window.deleteLater()
        settle()
