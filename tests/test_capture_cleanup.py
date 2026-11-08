"""Cleaning merged source tracks after archiving (opt-in) or on demand."""

from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

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


def make_capture_task(tmp_path: Path, *, merged: bool = True) -> DownloadTask:
    root = tmp_path / "capture" / "mse"
    root.mkdir(parents=True, exist_ok=True)
    video = root / "clip.mp4"
    audio = root / "clip.m4a"
    video.write_bytes(b"V" * 100)
    audio.write_bytes(b"A" * 50)
    merged_file = root / "clip.merged.mp4"
    if merged:
        merged_file.write_bytes(b"M" * 150)
    output = merged_file if merged else video
    tracks = [
        {"path": str(video), "kind": "video", "track": "v"},
        {"path": str(audio), "kind": "audio", "track": "a"},
    ]
    extra = [str(video), str(audio)] if merged else [str(audio)]
    return DownloadTask(
        url="https://example.com/watch",
        output=str(output),
        status=STATUS_COMPLETED,
        total_size=output.stat().st_size,
        downloaded=output.stat().st_size,
        options={
            "capture": "mse",
            "mse_session": "s-cleanup",
            "mse_merged": merged,
            "mse_tracks": tracks,
            "extra_paths": extra,
        },
    )


def test_cleanup_setting_roundtrip_and_dialog(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    assert settings.mse_delete_sources is False
    path = tmp_path / "config.json"
    settings.mse_delete_sources = True
    settings.save(path)
    assert AppSettings.load(path).mse_delete_sources is True

    dialog = SettingsDialog(settings)
    dialog.mse_delete_sources_check.setChecked(False)
    dialog.accept()
    assert settings.mse_delete_sources is False
    dialog.deleteLater()
    QApplication.processEvents()


def test_auto_cleanup_after_merged_archive(qapp, tmp_path):
    task = make_capture_task(tmp_path, merged=True)
    video = Path(task.options["mse_tracks"][0]["path"])
    audio = Path(task.options["mse_tracks"][1]["path"])
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"),
        mse_archive=True,
        mse_delete_sources=True,
    )
    manager = QueueManager(
        DownloadTableModel(), settings, capture_root=tmp_path / "capture"
    )
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        assert wait_until(
            lambda: not (manager.model.find(task.id) or task).options.get(
                "archive_pending"
            )
        )
        settled = manager.model.find(task.id)
        assert Path(settled.output).is_file()
        assert str(Path(settings.download_dir)) in settled.output
        assert not video.exists() and not audio.exists()
        assert settled.options["extra_paths"] == []
        assert settled.options["mse_tracks"] == []
        assert settled.options["sources_deleted"] is True
        assert settled.options["sources_deleted_count"] == 2
        assert settled.options["sources_freed_bytes"] == 150
    finally:
        manager.shutdown()
        settle()


def test_auto_cleanup_off_keeps_sources(qapp, tmp_path):
    task = make_capture_task(tmp_path, merged=True)
    video = Path(task.options["mse_tracks"][0]["path"])
    audio = Path(task.options["mse_tracks"][1]["path"])
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"),
        mse_archive=True,
        mse_delete_sources=False,
    )
    manager = QueueManager(
        DownloadTableModel(), settings, capture_root=tmp_path / "capture"
    )
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        assert wait_until(
            lambda: not (manager.model.find(task.id) or task).options.get(
                "archive_pending"
            )
        )
        assert video.exists() and audio.exists()
        assert not task.options.get("sources_deleted")
    finally:
        manager.shutdown()
        settle()


def test_auto_cleanup_skips_unmerged_capture(qapp, tmp_path):
    task = make_capture_task(tmp_path, merged=False)
    video = Path(task.options["mse_tracks"][0]["path"])
    audio = Path(task.options["mse_tracks"][1]["path"])
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"),
        mse_archive=True,
        mse_delete_sources=True,
    )
    manager = QueueManager(
        DownloadTableModel(), settings, capture_root=tmp_path / "capture"
    )
    try:
        manager.add_captured_task(task)
        manager.archive_capture_task(task)
        assert wait_until(
            lambda: not (manager.model.find(task.id) or task).options.get(
                "archive_pending"
            )
        )
        # No merged file: the primary track moved to the download folder and
        # the other track (the only remaining source copy) stays put.
        settled = manager.model.find(task.id)
        assert Path(settled.output).is_file()
        assert str(Path(settings.download_dir)) in settled.output
        assert not video.exists()
        assert audio.exists()
        assert not task.options.get("sources_deleted")
    finally:
        manager.shutdown()
        settle()


def test_manual_cleanup_keeps_the_primary_file(qapp, tmp_path):
    task = make_capture_task(tmp_path, merged=True)
    merged = Path(task.output)
    video = Path(task.options["mse_tracks"][0]["path"])
    audio = Path(task.options["mse_tracks"][1]["path"])
    settings = AppSettings(download_dir=str(tmp_path), mse_archive=False)
    manager = QueueManager(
        DownloadTableModel(), settings, capture_root=tmp_path / "capture"
    )
    try:
        manager.add_captured_task(task)
        assert set(manager.capture_source_paths(task)) == {video, audio}
        deleted, freed = manager.cleanup_capture_sources(task)
        assert (deleted, freed) == (2, 150)
        assert merged.exists()
        assert not video.exists() and not audio.exists()
        assert task.options["sources_deleted"] is True
    finally:
        manager.shutdown()
        settle()


def test_manual_cleanup_protects_outside_paths(qapp, tmp_path):
    task = make_capture_task(tmp_path, merged=True)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"NO")
    task.options["extra_paths"].append(str(outside))
    settings = AppSettings(download_dir=str(tmp_path), mse_archive=False)
    manager = QueueManager(
        DownloadTableModel(), settings, capture_root=tmp_path / "capture"
    )
    try:
        manager.add_captured_task(task)
        assert outside not in manager.capture_source_paths(task)
        manager.cleanup_capture_sources(task)
        assert outside.exists()
    finally:
        manager.shutdown()
        settle()


def test_cleanup_refuses_while_archiving(qapp, tmp_path):
    task = make_capture_task(tmp_path, merged=True)
    task.options["archiving"] = True
    settings = AppSettings(download_dir=str(tmp_path), mse_archive=False)
    manager = QueueManager(
        DownloadTableModel(), settings, capture_root=tmp_path / "capture"
    )
    try:
        manager.add_captured_task(task)
        assert manager.capture_source_paths(task) == []
        assert manager.cleanup_capture_sources(task) == (0, 0)
    finally:
        manager.shutdown()
        settle()


def test_main_window_cleanup_action(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window

    spool = tmp_path / "capture"
    monkeypatch.setattr(main_window, "capture_dir", lambda: spool)
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)
    settings = AppSettings(download_dir=str(tmp_path), mse_archive=False)
    window = main_window.MainWindow(settings)
    try:
        task = make_capture_task(tmp_path, merged=True)
        merged = Path(task.output)
        video = Path(task.options["mse_tracks"][0]["path"])
        audio = Path(task.options["mse_tracks"][1]["path"])
        window.manager.add_captured_task(task)
        window.table.selectRow(0)
        QApplication.processEvents()
        assert window.cleanup_sources_action.isEnabled()

        infos: list[str] = []

        class FakeBox:
            Icon = QMessageBox.Icon
            StandardButton = QMessageBox.StandardButton

            def __init__(self, parent=None):
                pass

            def setWindowTitle(self, *_a):
                pass

            def setText(self, *_a):
                pass

            def setIcon(self, *_a):
                pass

            def setStandardButtons(self, *_a):
                pass

            def setDefaultButton(self, *_a):
                pass

            def exec(self):
                return QMessageBox.StandardButton.Yes

            @staticmethod
            def information(_parent, _title, text, *a, **k):
                infos.append(text)

        monkeypatch.setattr(main_window, "QMessageBox", FakeBox)
        window.cleanup_capture_sources()
        assert not video.exists() and not audio.exists()
        assert merged.exists()
        assert infos and "2" in infos[0]
    finally:
        window.manager.shutdown()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window._really_quit = True
        window.close()
        window.deleteLater()
        settle()
