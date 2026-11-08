"""Shut down when every download has finished (policy, queue and GUI wiring)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from personal_download_manager import shutdown
from personal_download_manager.gui.dialogs import SettingsDialog
from personal_download_manager.gui.manager import QueueManager
from personal_download_manager.gui.model import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_QUEUED,
    STATUS_RETRYING,
    STATUS_SCHEDULED,
    DownloadTableModel,
    DownloadTask,
)
from personal_download_manager.gui.settings import AppSettings
from server import Route, make_content


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def wait_until(predicate, timeout: float = 20.0) -> bool:
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


def settle(delay: float = 0.05) -> None:
    QApplication.processEvents()
    time.sleep(delay)
    QApplication.processEvents()


def make_manager(tmp_path, **overrides):
    """QueueManager whose shutdown runner only records the command."""
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=2,
        default_connections=2,
        shutdown_delay=2,
        **overrides,
    )
    calls: list[list[str]] = []
    manager = QueueManager(
        DownloadTableModel(), settings, shutdown_runner=calls.append
    )
    return manager, calls


def add_task(manager, name: str, status: str) -> DownloadTask:
    task = DownloadTask(
        url=f"https://example.com/{name}",
        output=str(manager.settings.download_dir) + "/" + name,
        status=status,
    )
    manager.model.add_task(task)
    return task


# ----------------------------------------------------------------------
# policy
# ----------------------------------------------------------------------
def test_policy_requires_a_finished_queue():
    assert shutdown.should_shut_down([]) is False
    assert shutdown.should_shut_down([STATUS_COMPLETED]) is True
    assert shutdown.should_shut_down(
        [STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED]
    ) is True
    # Nothing succeeded: keep the machine on so the user can look at it.
    assert shutdown.should_shut_down([STATUS_FAILED]) is False
    assert shutdown.should_shut_down([STATUS_CANCELLED]) is False


@pytest.mark.parametrize(
    "status",
    [
        STATUS_QUEUED,
        STATUS_DOWNLOADING,
        STATUS_PAUSED,
        STATUS_RETRYING,
        STATUS_SCHEDULED,
        "unknown-status",
    ],
)
def test_policy_blocking_statuses(status):
    assert (
        shutdown.should_shut_down([STATUS_COMPLETED, status]) is False
    ), f"{status} must keep the machine on"


def test_shutdown_command_is_an_argument_list():
    assert shutdown.shutdown_command("win32") == [
        "shutdown",
        "/s",
        "/t",
        "0",
    ]
    assert shutdown.shutdown_command("win32", 30) == [
        "shutdown",
        "/s",
        "/t",
        "30",
    ]
    assert shutdown.shutdown_command("linux") == ["shutdown", "-h", "now"]
    assert shutdown.shutdown_command("darwin") == ["shutdown", "-h", "now"]


def test_run_shutdown_never_uses_a_shell(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs

    monkeypatch.setattr(shutdown.subprocess, "run", fake_run)
    shutdown.run_shutdown(["shutdown", "/s", "/t", "0"])
    assert seen["command"] == ["shutdown", "/s", "/t", "0"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["check"] is True


def test_shutdown_delay_setting_clamps_and_roundtrips(tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), shutdown_delay=0)
    assert settings.shutdown_delay == 1
    settings.shutdown_delay = 99999
    settings.__post_init__()
    assert settings.shutdown_delay == 3600

    settings.shutdown_delay = 45
    path = tmp_path / "config.json"
    settings.save(path)
    assert AppSettings.load(path).shutdown_delay == 45


def test_settings_dialog_saves_shutdown_delay(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), shutdown_delay=60)
    dialog = SettingsDialog(settings)
    dialog.shutdown_delay.setValue(25)
    dialog.accept()
    assert settings.shutdown_delay == 25
    dialog.deleteLater()
    QApplication.processEvents()


# ----------------------------------------------------------------------
# queue manager
# ----------------------------------------------------------------------
def test_countdown_fires_once_when_the_queue_is_done(qapp, tmp_path):
    manager, calls = make_manager(tmp_path)
    add_task(manager, "done.bin", STATUS_COMPLETED)
    seconds: list[int] = []
    manager.shutdown_countdown.connect(seconds.append)

    manager.arm_shutdown()
    assert manager.shutdown_armed
    assert seconds == [2]

    manager._tick_shutdown()
    assert seconds == [2, 1]
    manager._tick_shutdown()
    assert calls == [shutdown.shutdown_command()]
    assert seconds[-1] == 0
    assert manager.shutdown_armed is False

    # A fired shutdown is one-shot: further ticks must not repeat it.
    manager._tick_shutdown()
    assert len(calls) == 1
    manager.shutdown()
    settle()


def test_cancel_stops_the_countdown(qapp, tmp_path):
    manager, calls = make_manager(tmp_path)
    add_task(manager, "done.bin", STATUS_COMPLETED)
    manager.arm_shutdown()
    manager.cancel_shutdown()
    assert manager.shutdown_armed is False
    manager._tick_shutdown()
    manager._tick_shutdown()
    assert calls == []
    manager.shutdown()
    settle()


def test_no_tasks_never_shuts_down(qapp, tmp_path):
    manager, calls = make_manager(tmp_path)
    seconds: list[int] = []
    manager.shutdown_countdown.connect(seconds.append)
    manager.arm_shutdown()
    assert manager.shutdown_armed
    assert seconds == []
    manager._tick_shutdown()
    assert calls == []
    manager.shutdown()
    settle()


def test_paused_task_blocks_then_removal_rearms(qapp, tmp_path):
    manager, calls = make_manager(tmp_path)
    add_task(manager, "done.bin", STATUS_COMPLETED)
    paused = add_task(manager, "later.bin", STATUS_PAUSED)
    seconds: list[int] = []
    manager.shutdown_countdown.connect(seconds.append)

    manager.arm_shutdown()
    assert seconds == [], "a paused download must keep the machine on"

    manager.remove(paused.id)
    assert seconds and seconds[-1] == 2
    manager._tick_shutdown()
    manager._tick_shutdown()
    assert calls == [shutdown.shutdown_command()]
    manager.shutdown()
    settle()


def test_mse_captures_do_not_trigger_shutdown(qapp, tmp_path):
    manager, calls = make_manager(tmp_path)
    manager.model.add_task(
        DownloadTask(
            url="mse://capture",
            output=str(tmp_path / "capture.mp4"),
            status=STATUS_COMPLETED,
            options={"capture": "mse", "mse_session": "s"},
        )
    )
    seconds: list[int] = []
    manager.shutdown_countdown.connect(seconds.append)
    manager.arm_shutdown()
    assert seconds == [], "an imported capture is not a finished download"
    manager._tick_shutdown()
    assert calls == []
    manager.shutdown()
    settle()


def test_new_task_cancels_the_countdown(qapp, tmp_path):
    manager, calls = make_manager(tmp_path)
    add_task(manager, "done.bin", STATUS_COMPLETED)
    seconds: list[int] = []
    manager.shutdown_countdown.connect(seconds.append)

    manager.arm_shutdown()
    assert seconds == [2]
    task = DownloadTask(
        url="https://example.com/new.bin",
        output=str(tmp_path / "new.bin"),
    )
    manager.add_task(task, start=False)
    assert seconds[-1] == 0
    manager._tick_shutdown()
    assert calls == []
    manager.shutdown()
    settle()


def test_failed_runner_disarms_and_reports(qapp, tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path), shutdown_delay=1
    )

    def boom(command):
        raise OSError("access denied")

    manager = QueueManager(
        DownloadTableModel(), settings, shutdown_runner=boom
    )
    add_task(manager, "done.bin", STATUS_COMPLETED)
    failures: list[str] = []
    manager.shutdown_failed.connect(failures.append)
    manager.arm_shutdown()
    manager._tick_shutdown()
    assert failures and "access denied" in failures[0]
    assert manager.shutdown_armed is False
    manager.shutdown()
    settle()


def test_real_download_triggers_the_shutdown(qapp, server, tmp_path):
    data = make_content(256 * 1024, seed=310)
    server.add("/shutdown.bin", Route(content=data, etag='"sd"'))
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=2,
        default_connections=2,
        shutdown_delay=1,
    )
    calls: list[list[str]] = []
    manager = QueueManager(
        DownloadTableModel(), settings, shutdown_runner=calls.append
    )
    task = DownloadTask(
        url=server.url("/shutdown.bin"),
        output=str(tmp_path / "shutdown.bin"),
        options={"connections": 2},
    )
    manager.add_task(task)
    manager.arm_shutdown()
    assert wait_until(lambda: bool(calls), 20), "countdown should run after completion"
    assert (tmp_path / "shutdown.bin").read_bytes() == data
    assert calls[0] == shutdown.shutdown_command()
    manager.shutdown()
    settle()


# ----------------------------------------------------------------------
# main window wiring
# ----------------------------------------------------------------------
def test_main_window_toggle_and_countdown_label(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window

    monkeypatch.setattr(main_window, "capture_dir", lambda: tmp_path / "capture")
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)

    settings = AppSettings(download_dir=str(tmp_path), shutdown_delay=30)
    window = main_window.MainWindow(settings)
    try:
        assert window.shutdown_action.isCheckable()
        window.shutdown_action.trigger()  # user enables the feature
        assert window.manager.shutdown_armed
        assert window.shutdown_action.isChecked()

        window.model.add_task(
            DownloadTask(
                url="https://example.com/done.bin",
                output=str(tmp_path / "done.bin"),
                status=STATUS_COMPLETED,
            )
        )
        window.manager._changed()
        assert "30" in window.shutdown_label.text()
        assert window._shutdown_box is not None

        window.shutdown_action.trigger()  # user cancels
        assert not window.manager.shutdown_armed
        assert not window.shutdown_action.isChecked()
    finally:
        window.manager.cancel_shutdown()
        window._close_shutdown_box()
        window._really_quit = True
        window.close()
        window.deleteLater()
        settle()
