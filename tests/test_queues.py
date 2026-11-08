"""Per-queue concurrency and time-of-day bandwidth windows."""

from __future__ import annotations

import os
import threading
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

import personal_download_manager.gui.manager as manager_module

from personal_download_manager.bandwidth import limit_at, parse_hhmm, parse_window, parse_windows
from personal_download_manager.gui.dialogs import BandwidthDialog, QueueDialog
from personal_download_manager.gui.manager import QueueManager
from personal_download_manager.gui.model import (
    COL_QUEUE,
    STATUS_QUEUED,
    DownloadTableModel,
    DownloadTask,
    TaskFilterProxy,
)
from personal_download_manager.gui.settings import AppSettings


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


class _FakeWorker(QThread):
    """Stands in for DownloadWorker so scheduler tests never touch sockets."""

    progress = Signal(str, dict)
    succeeded = Signal(str, dict)
    failed = Signal(str, str, bool)
    cancelled = Signal(str)

    def __init__(self, task, settings, governor=None, parent=None) -> None:
        super().__init__(parent)
        self.task = task
        self._stop = threading.Event()

    def run(self) -> None:  # noqa: D401
        self._stop.wait(30)
        self.cancelled.emit(self.task.id)

    def cancel(self) -> None:
        self._stop.set()


@pytest.fixture
def fake_workers(monkeypatch):
    monkeypatch.setattr(manager_module, "DownloadWorker", _FakeWorker)


# ----------------------------------------------------------------------
# settings / scheduling
# ----------------------------------------------------------------------
def test_settings_queue_limits_and_cleanup(tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=2,
        queues=[
            {"name": "夜间", "concurrent": 3},
            {"name": "主队列", "concurrent": 9},  # reserved, ignored
            {"name": "夜间", "concurrent": 1},     # duplicate, ignored
            {"name": "", "concurrent": 1},         # empty, ignored
            {"name": "坏值", "concurrent": "x"},    # invalid, ignored
        ],
    )
    limits = settings.queue_limits()
    assert limits == {"主队列": 2, "夜间": 3}
    assert settings.queue_names() == ["主队列", "夜间"]

    path = tmp_path / "config.json"
    settings.save(path)
    loaded = AppSettings.load(path)
    assert loaded.queue_limits() == {"主队列": 2, "夜间": 3}


def _task(tmp_path, name: str, queue: str) -> DownloadTask:
    return DownloadTask(
        url=f"http://example.test/{name}",
        output=str(tmp_path / name),
        queue=queue,
    )


def test_manager_runs_one_task_per_queue(qapp, tmp_path, fake_workers):
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=1,
        queues=[{"name": "夜间", "concurrent": 1}],
    )
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    try:
        main_a = _task(tmp_path, "a.bin", "主队列")
        main_b = _task(tmp_path, "b.bin", "主队列")
        night = _task(tmp_path, "c.bin", "夜间")
        manager.add_task(main_a)
        manager.add_task(main_b)
        manager.add_task(night)

        assert main_a.id in manager.workers
        assert main_b.id not in manager.workers
        assert model.find(main_b.id).status == STATUS_QUEUED
        assert night.id in manager.workers
        assert len(manager.workers) == 2
    finally:
        manager.shutdown()


def test_move_task_frees_a_queue_slot(qapp, tmp_path, fake_workers):
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=1,
        queues=[{"name": "夜间", "concurrent": 1}],
    )
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    try:
        first = _task(tmp_path, "m1.bin", "主队列")
        second = _task(tmp_path, "m2.bin", "主队列")
        manager.add_task(first)
        manager.add_task(second)
        assert second.id not in manager.workers

        manager.move_task(second.id, "夜间")
        assert model.find(second.id).queue == "夜间"
        assert second.id in manager.workers
        assert len(manager.workers) == 2
    finally:
        manager.shutdown()


def test_unknown_queue_falls_back_to_main(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=1)
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    try:
        task = DownloadTask(
            url="http://127.0.0.1:9/x", output=str(tmp_path / "x"), queue="不存在"
        )
        manager.add_task(task, start=False)
        assert model.find(task.id).queue == "主队列"

        manager.load_tasks(
            [{"url": "http://x/a", "output": str(tmp_path / "a"), "queue": "幽灵"}]
        )
        assert [t.queue for t in model.tasks()] == ["主队列"]
    finally:
        manager.shutdown()


def test_queue_column_and_filter(qapp):
    model = DownloadTableModel()
    proxy = TaskFilterProxy()
    proxy.setSourceModel(model)
    model.add_task(DownloadTask(url="http://x/a", output="a", queue="主队列"))
    model.add_task(DownloadTask(url="http://x/b", output="b", queue="夜间"))

    assert DownloadTableModel._display(model.task_at(1), COL_QUEUE) == "夜间"
    proxy.set_queue("夜间")
    assert proxy.rowCount() == 1
    proxy.set_queue("")
    assert proxy.rowCount() == 2


# ----------------------------------------------------------------------
# bandwidth windows
# ----------------------------------------------------------------------
def test_parse_helpers():
    assert parse_hhmm("09:30") == 570
    assert parse_hhmm("24:00") is None
    assert parse_hhmm("9:5") == 545
    assert parse_hhmm("nope") is None
    assert parse_window({"start": "09:00", "end": "09:00"}) is None
    assert parse_window({"start": "bad", "end": "10:00"}) is None
    windows = parse_windows(
        [
            {"start": "09:00", "end": "18:00", "limit": "1M"},
            {"start": "09:00", "end": "18:00", "limit": None},
            {"start": "bad", "end": "18:00", "limit": 1},
        ]
    )
    assert len(windows) == 2
    assert windows[0].limit == 1024 * 1024


def test_limit_at_day_window():
    windows = parse_windows([{"start": "09:00", "end": "18:00", "limit": "1M"}])
    default = 5 * 1024 * 1024
    assert limit_at(datetime(2026, 1, 1, 10, 0), windows, default) == 1024 * 1024
    assert limit_at(datetime(2026, 1, 1, 9, 0), windows, default) == 1024 * 1024
    assert limit_at(datetime(2026, 1, 1, 18, 0), windows, default) == default
    assert limit_at(datetime(2026, 1, 1, 8, 59), windows, default) == default


def test_limit_at_wrapping_window():
    windows = parse_windows([{"start": "22:00", "end": "06:00", "limit": "2M"}])
    default = 5 * 1024 * 1024
    assert limit_at(datetime(2026, 1, 1, 23, 0), windows, default) == 2 * 1024 * 1024
    assert limit_at(datetime(2026, 1, 1, 5, 59), windows, default) == 2 * 1024 * 1024
    assert limit_at(datetime(2026, 1, 1, 6, 0), windows, default) == default
    assert limit_at(datetime(2026, 1, 1, 12, 0), windows, default) == default


def test_limit_at_overlap_and_unlimited():
    windows = parse_windows(
        [
            {"start": "00:00", "end": "23:59", "limit": "3M"},
            {"start": "09:00", "end": "18:00", "limit": "1M"},
        ]
    )
    assert limit_at(datetime(2026, 1, 1, 10, 0), windows, None) == 1024 * 1024

    unlimited = parse_windows(
        [{"start": "00:00", "end": "23:59", "limit": None}]
    )
    assert limit_at(datetime(2026, 1, 1, 10, 0), unlimited, 5 * 1024 * 1024) is None


# ----------------------------------------------------------------------
# dialogs / main window
# ----------------------------------------------------------------------
def test_queue_dialog_parses_rows(qapp):
    dialog = QueueDialog([{"name": "夜间", "concurrent": 3}])
    dialog._add_row({"name": "白天", "concurrent": 1})
    dialog.accept()
    assert dialog.queues == [
        {"name": "夜间", "concurrent": 3},
        {"name": "白天", "concurrent": 1},
    ]


def test_bandwidth_dialog_parses_rows(qapp):
    dialog = BandwidthDialog([{"start": "09:00", "end": "18:00", "limit": "1M"}])
    dialog._add_row({"start": "22:00", "end": "06:00", "limit": ""})
    dialog.accept()
    assert dialog.windows == [
        {"start": "09:00", "end": "18:00", "limit": 1024 * 1024},
        {"start": "22:00", "end": "06:00", "limit": None},
    ]


def test_main_window_applies_bandwidth_and_lists_queues(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window

    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)
    monkeypatch.setattr(main_window, "capture_dir", lambda: tmp_path / "capture")
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=2,
        global_speed_limit=5 * 1024 * 1024,
        queues=[{"name": "夜间", "concurrent": 1}],
        bandwidth_windows=[
            {"start": "09:00", "end": "18:00", "limit": "1M"},
        ],
    )
    settings.save = lambda path=None: None
    window = main_window.MainWindow(settings)
    try:
        assert window.queue_combo.findData("夜间") >= 0
        window._apply_bandwidth(datetime(2026, 1, 1, 10, 0))
        assert window.governor.global_speed_limit == 1024 * 1024
        window._apply_bandwidth(datetime(2026, 1, 1, 20, 0))
        assert window.governor.global_speed_limit == 5 * 1024 * 1024
    finally:
        window.manager.shutdown()
        for timer in (
            window._capture_timer,
            window._bandwidth_timer,
            window._clip_timer,
        ):
            timer.stop()
        window.hide()
        window.deleteLater()
        # Let Qt actually destroy the window now; otherwise a Python-owned
        # wrapper can be freed first and the pending DeleteLater crashes the
        # next test's event loop.
        QApplication.processEvents()
