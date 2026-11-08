"""Background archival of finished MSE captures into the download folder."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from personal_download_manager.gui import archive as archive_module
from personal_download_manager.gui import i18n
from personal_download_manager.gui.dialogs import SettingsDialog
from personal_download_manager.gui.manager import QueueManager
from personal_download_manager.gui.model import (
    STATUS_COMPLETED,
    DownloadTableModel,
    DownloadTask,
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


def wait_until(predicate, timeout: float = 10.0) -> bool:
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(25)
    state = {"done": False}

    def tick() -> None:
        if predicate():
            state["done"] = True
            timer.stop()
            loop.quit()

    timer.timeout.connect(tick)
    timer.start()
    QTimer.singleShot(int(timeout * 1000), loop.quit)
    loop.exec()
    timer.stop()
    return state["done"]


def make_source(tmp_path: Path, data: bytes = b"CAPTURE") -> Path:
    folder = tmp_path / "capture" / "mse"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "clip.mp4"
    path.write_bytes(data)
    return path


def make_task(source: Path, *, sha256: str | None = None) -> DownloadTask:
    return DownloadTask(
        url="https://example.com/watch",
        output=str(source),
        status=STATUS_COMPLETED,
        total_size=source.stat().st_size,
        downloaded=source.stat().st_size,
        options={
            "capture": "mse",
            "mse_session": "s1",
            "mse_tracks": [],
            "mse_primary_sha256": sha256,
        },
    )


# ----------------------------------------------------------------------
# pure file operation
# ----------------------------------------------------------------------
def test_same_volume_move_is_atomic(tmp_path):
    source = make_source(tmp_path)
    dest = tmp_path / "downloads" / "video"
    target = archive_module.archive_capture(
        source, dest, "clip.mp4", expected_size=source.stat().st_size
    )
    assert target == dest / "clip.mp4"
    assert target.read_bytes() == b"CAPTURE"
    assert not source.exists()


def test_archived_name_is_unique(tmp_path):
    source = make_source(tmp_path)
    dest = tmp_path / "downloads"
    dest.mkdir()
    (dest / "clip.mp4").write_bytes(b"OLD")
    target = archive_module.archive_capture(source, dest, "clip.mp4")
    assert target == dest / "clip (1).mp4"
    assert target.read_bytes() == b"CAPTURE"


def test_size_mismatch_keeps_the_source(tmp_path):
    source = make_source(tmp_path)
    with pytest.raises(archive_module.ArchiveError):
        archive_module.archive_capture(
            source, tmp_path / "downloads", "clip.mp4", expected_size=9999
        )
    assert source.exists()
    assert not (tmp_path / "downloads" / "clip.mp4").exists()


def test_cross_volume_copy_verifies_hash(tmp_path, monkeypatch):
    source = make_source(tmp_path, b"DATA" * 1000)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    dest = tmp_path / "downloads"
    real_replace = os.replace

    def fake_replace(src, dst):
        if Path(src) == source:
            raise OSError("cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(archive_module.os, "replace", fake_replace)
    target = archive_module.archive_capture(
        source,
        dest,
        "clip.mp4",
        expected_size=source.stat().st_size,
        sha256=digest,
    )
    assert target.read_bytes() == b"DATA" * 1000
    assert not source.exists()
    assert not list(dest.glob("*.pdm-archive.part"))


def test_cross_volume_hash_mismatch_keeps_source(tmp_path, monkeypatch):
    source = make_source(tmp_path, b"DATA")
    real_replace = os.replace

    def fake_replace(src, dst):
        if Path(src) == source:
            raise OSError("cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(archive_module.os, "replace", fake_replace)
    with pytest.raises(archive_module.ArchiveError):
        archive_module.archive_capture(
            source,
            tmp_path / "downloads",
            "clip.mp4",
            expected_size=4,
            sha256="0" * 64,
        )
    assert source.exists()
    assert not (tmp_path / "downloads" / "clip.mp4").exists()
    assert not list((tmp_path / "downloads").glob("*.pdm-archive.part"))


def test_cancel_before_copy_keeps_source(tmp_path):
    source = make_source(tmp_path)
    event = threading.Event()
    event.set()
    with pytest.raises(archive_module.ArchiveCancelled):
        archive_module.archive_capture(
            source, tmp_path / "downloads", "clip.mp4", cancel_event=event
        )
    assert source.exists()
    assert not (tmp_path / "downloads" / "clip.mp4").exists()


# ----------------------------------------------------------------------
# settings
# ----------------------------------------------------------------------
def test_mse_archive_setting_roundtrip(tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    assert settings.mse_archive is True
    settings.mse_archive = False
    path = tmp_path / "config.json"
    settings.save(path)
    assert AppSettings.load(path).mse_archive is False


def test_settings_dialog_saves_mse_archive(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), mse_archive=True)
    dialog = SettingsDialog(settings)
    dialog.mse_archive_check.setChecked(False)
    dialog.accept()
    assert settings.mse_archive is False
    dialog.deleteLater()
    QApplication.processEvents()


# ----------------------------------------------------------------------
# queue manager
# ----------------------------------------------------------------------
def test_manager_moves_capture_and_updates_task(qapp, tmp_path):
    source = make_source(tmp_path)
    task = make_task(source, sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"), mse_archive=True
    )
    manager = QueueManager(DownloadTableModel(), settings)
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        assert wait_until(
            lambda: not (manager.model.find(task.id) or task).options.get(
                "archive_pending"
            )
        )
        settled = manager.model.find(task.id)
        assert settled is not None
        assert settled.output != str(source)
        assert Path(settled.output).is_file()
        assert not source.exists()
        folder = settings.category_folder(settled.filename)
        expected = Path(settings.download_dir)
        if folder:
            expected = expected.joinpath(*folder.split("/"))
        assert Path(settled.output).parent == expected
        assert settled.options["archiving"] is False
        assert settled.options["captured_path"] == str(source)
        assert settled.status_label == "已捕获"
    finally:
        manager.shutdown()
        settle()


def test_manager_archive_failure_keeps_capture(qapp, tmp_path, monkeypatch):
    source = make_source(tmp_path)
    task = make_task(source)
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"), mse_archive=True
    )
    manager = QueueManager(DownloadTableModel(), settings)
    failures: list[str] = []
    manager.archive_failed.connect(lambda _tid, message: failures.append(message))

    def boom(*args, **kwargs):
        raise OSError("destination is read-only")

    monkeypatch.setattr(archive_module, "archive_capture", boom)
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        assert wait_until(
            lambda: bool((manager.model.find(task.id) or task).options.get(
                "archive_error"
            ))
        )
        kept = manager.model.find(task.id)
        assert kept is not None
        assert kept.output == str(source)
        assert source.exists()
        assert kept.options["archiving"] is False
        assert failures and "read-only" in failures[0]
    finally:
        manager.shutdown()
        settle()


def test_removal_waits_for_archive_then_deletes(qapp, tmp_path, monkeypatch):
    source = make_source(tmp_path)
    task = make_task(source)
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"), mse_archive=True
    )
    manager = QueueManager(DownloadTableModel(), settings)
    real = archive_module.archive_capture

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(archive_module, "archive_capture", slow)
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        manager.remove(task.id, delete_files=True)
        assert manager.model.find(task.id) is not None, "row waits for the thread"
        assert wait_until(lambda: manager.model.find(task.id) is None)
        assert not source.exists()
        dest = Path(settings.download_dir)
        assert not list(dest.rglob("clip*.mp4"))
    finally:
        manager.shutdown()
        settle()


def test_pending_archive_resumes_on_start(qapp, tmp_path):
    source = make_source(tmp_path)
    task = make_task(source)
    task.options["archive_pending"] = True
    task.options["archive_source"] = str(source)
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"), mse_archive=True
    )
    manager = QueueManager(DownloadTableModel(), settings)
    try:
        manager.add_captured_task(task)
        manager.resume_capture_archives()
        assert wait_until(
            lambda: not (manager.model.find(task.id) or task).options.get(
                "archive_pending"
            )
        )
        assert not source.exists()
        assert Path(manager.model.find(task.id).output).is_file()
    finally:
        manager.shutdown()
        settle()


def test_disabled_setting_leaves_the_capture_in_place(qapp, tmp_path):
    source = make_source(tmp_path)
    task = make_task(source)
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"), mse_archive=False
    )
    manager = QueueManager(DownloadTableModel(), settings)
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        assert manager._archive_workers == {}
        assert task.output == str(source)
        assert not task.options.get("archiving")
    finally:
        manager.shutdown()
        settle()


def test_archive_tooltip_states():
    source = Path("clip.mp4")
    task = DownloadTask(
        url="https://example.com/", output=str(source), status=STATUS_COMPLETED,
        options={"capture": "mse", "archiving": True, "mse_tracks": []},
    )
    assert "正在归档到下载目录…" in DownloadTableModel._status_tooltip(task)
    task.options["archiving"] = False
    task.options["archive_error"] = "denied"
    assert "归档失败：denied" in DownloadTableModel._status_tooltip(task)


# ----------------------------------------------------------------------
# main window integration
# ----------------------------------------------------------------------
def test_main_window_archives_an_imported_capture(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window

    spool = tmp_path / "capture"
    monkeypatch.setattr(main_window, "capture_dir", lambda: spool)
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)

    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"), mse_archive=True
    )
    window = main_window.MainWindow(settings)
    try:
        root = spool / "mse"
        root.mkdir(parents=True)
        merged = root / "win.merged.mp4"
        merged.write_bytes(b"WINDOW")
        import json

        (root / "mse-session-win.json").write_text(
            json.dumps(
                {
                    "kind": "mse",
                    "version": 1,
                    "session": "win",
                    "created_at": 1790000000.0,
                    "source_url": "https://example.com/watch",
                    "title": "Window",
                    "files": [],
                    "merged": {
                        "path": str(merged),
                        "size": len(b"WINDOW"),
                        "sha256": hashlib.sha256(b"WINDOW").hexdigest(),
                        "kind": "video",
                        "mime": "video/mp4",
                    },
                    "merge_error": None,
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        window._poll_captures()
        tasks = window.model.tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert wait_until(
            lambda: not task.options.get("archive_pending") and task.output != str(merged)
        )
        assert not merged.exists()
        assert Path(task.output).is_file()
        assert str(Path(settings.download_dir)) in task.output
    finally:
        window.manager.shutdown()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window._really_quit = True
        window.close()
        window.deleteLater()
        settle()
