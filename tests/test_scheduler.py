"""Planned starts and the queue-level retry policy."""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from personal_download_manager.errors import (
    AuthRequired,
    Cancelled,
    ChecksumMismatch,
    HTTPStatusError,
    ProbeError,
    RetryableError,
    is_retryable,
)
from personal_download_manager.gui.manager import QueueManager, retry_delay
from personal_download_manager.gui.model import (
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PAUSED,
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


def make_manager(tmp_path, **kwargs) -> QueueManager:
    settings = AppSettings(
        download_dir=str(tmp_path),
        max_concurrent=2,
        default_connections=2,
        **kwargs,
    )
    return QueueManager(DownloadTableModel(), settings)


# ----------------------------------------------------------------------
# policy (no Qt event loop needed)
# ----------------------------------------------------------------------
def test_is_retryable_classification():
    assert is_retryable(RetryableError("connection reset"))
    assert is_retryable(HTTPStatusError(429, "Too Many Requests", "u"))
    assert is_retryable(HTTPStatusError(503, "Unavailable", "u"))
    assert is_retryable(ProbeError("timed out", transient=True))

    assert not is_retryable(HTTPStatusError(404, "Not Found", "u"))
    assert not is_retryable(HTTPStatusError(403, "Forbidden", "u"))
    assert not is_retryable(AuthRequired("need credentials"))
    assert not is_retryable(ChecksumMismatch("a" * 64, "b" * 64))
    assert not is_retryable(ProbeError("invalid URL"))
    assert not is_retryable(Cancelled("stop"))
    assert not is_retryable(ValueError("bug"))


def test_retry_delay_backs_off_and_caps():
    assert retry_delay(10, 0) == 10
    assert retry_delay(10, 1) == 20
    assert retry_delay(10, 3) == 80
    assert retry_delay(10, 20) == 3600  # capped at one hour
    assert retry_delay(0, 0) == 1       # never a busy loop


def test_settings_clamp_auto_retry(tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path), auto_retry_max=99, auto_retry_delay=0
    )
    assert settings.auto_retry_max == 10
    assert settings.auto_retry_delay == 1

    path = tmp_path / "config.json"
    settings.save(path)
    loaded = AppSettings.load(path)
    assert loaded.auto_retry is True
    assert loaded.auto_retry_max == 10
    assert loaded.auto_retry_delay == 1


# ----------------------------------------------------------------------
# failure retry queue
# ----------------------------------------------------------------------
def test_transient_failure_is_retried_and_completes(qapp, server, tmp_path):
    data = make_content(1024 * 1024, seed=90)
    # The probe (first request) gets one 503, then the server is healthy.
    server.add("/flaky.bin", Route(content=data, etag='"flaky"', fail_times=1))
    manager = make_manager(tmp_path, auto_retry_max=2, auto_retry_delay=1)
    task = DownloadTask(
        url=server.url("/flaky.bin"),
        output=str(tmp_path / "flaky.bin"),
        options={"connections": 2, "min_segment": "256K", "min_split": "256K"},
    )
    manager.add_task(task)

    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status
        == STATUS_RETRYING,
        10,
    ), "transient failure should wait for a retry, not fail outright"
    waiting = manager.model.find(task.id)
    assert waiting.auto_retries == 1
    assert waiting.next_retry_at is not None
    assert waiting.next_retry_at > time.time() - 1

    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    finished = manager.model.find(task.id)
    assert finished.status == STATUS_COMPLETED, finished.error
    assert finished.auto_retries == 1
    assert (tmp_path / "flaky.bin").read_bytes() == data


def test_permanent_failure_is_not_retried(qapp, server, tmp_path):
    manager = make_manager(tmp_path, auto_retry_max=3, auto_retry_delay=1)
    task = DownloadTask(
        url=server.url("/missing.bin"),
        output=str(tmp_path / "missing.bin"),
    )
    manager.add_task(task)

    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status
        in (STATUS_FAILED, STATUS_RETRYING),
        10,
    )
    manager.shutdown()
    finished = manager.model.find(task.id)
    assert finished.status == STATUS_FAILED
    assert "404" in (finished.error or "")
    assert finished.auto_retries == 0
    assert finished.next_retry_at is None


def test_retry_budget_is_exhausted(qapp, server, tmp_path):
    server.add("/down.bin", Route(content=b"x" * 1024, fail_times=99))
    manager = make_manager(tmp_path, auto_retry_max=1, auto_retry_delay=1)
    task = DownloadTask(
        url=server.url("/down.bin"),
        output=str(tmp_path / "down.bin"),
    )
    manager.add_task(task)

    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status
        in (STATUS_RETRYING, STATUS_FAILED),
        10,
    )
    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status == STATUS_FAILED, 20
    )
    manager.shutdown()
    finished = manager.model.find(task.id)
    assert finished.auto_retries == 1
    assert finished.next_retry_at is None
    assert "503" in (finished.error or "")


# ----------------------------------------------------------------------
# planned starts
# ----------------------------------------------------------------------
def test_scheduled_task_waits_for_its_time(qapp, server, tmp_path):
    data = make_content(256 * 1024, seed=91)
    server.add("/later.bin", Route(content=data, etag='"later"'))
    manager = make_manager(tmp_path)
    when = time.time() + 1.5
    task = DownloadTask(
        url=server.url("/later.bin"),
        output=str(tmp_path / "later.bin"),
        scheduled_at=when,
        options={"connections": 2, "min_segment": "64K", "min_split": "64K"},
    )
    manager.add_task(task, start=True)

    assert (manager.model.find(task.id) or task).status == STATUS_SCHEDULED
    assert task.id not in manager.workers
    assert not (tmp_path / "later.bin").exists()

    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status == STATUS_DOWNLOADING,
        10,
    )
    assert wait_until(
        lambda: (manager.model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    finished = manager.model.find(task.id)
    assert finished.status == STATUS_COMPLETED, finished.error
    assert (tmp_path / "later.bin").read_bytes() == data


def test_resume_starts_a_scheduled_task_now(qapp, tmp_path):
    manager = make_manager(tmp_path)
    task = DownloadTask(url="http://127.0.0.1:9/never", output=str(tmp_path / "n"))
    manager.add_task(task, start=False)
    manager.schedule_task(task.id, time.time() + 3600)
    assert (manager.model.find(task.id) or task).status == STATUS_SCHEDULED

    manager.resume(task.id)
    manager.shutdown()
    current = manager.model.find(task.id)
    assert current.scheduled_at is None
    assert current.status != STATUS_SCHEDULED


def test_pause_clears_a_plan(qapp, tmp_path):
    manager = make_manager(tmp_path)
    task = DownloadTask(url="http://127.0.0.1:9/never", output=str(tmp_path / "n"))
    manager.add_task(task, start=False)
    manager.schedule_task(task.id, time.time() + 3600)
    manager.pause(task.id)
    manager.shutdown()
    current = manager.model.find(task.id)
    assert current.status == STATUS_PAUSED
    assert current.scheduled_at is None


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------
def test_load_tasks_restores_scheduler_state(qapp, tmp_path):
    manager = make_manager(tmp_path)
    future = time.time() + 3600
    manager.load_tasks(
        [
            {
                "url": "http://x/a",
                "output": str(tmp_path / "a"),
                "status": STATUS_SCHEDULED,
                "scheduled_at": future,
            },
            {
                "url": "http://x/b",
                "output": str(tmp_path / "b"),
                "status": STATUS_SCHEDULED,
                "scheduled_at": time.time() - 60,
            },
            {
                "url": "http://x/c",
                "output": str(tmp_path / "c"),
                "status": STATUS_RETRYING,
                "next_retry_at": time.time() + 30,
                "auto_retries": 2,
            },
        ]
    )
    manager.shutdown()
    by_url = {task.url: task for task in manager.model.tasks()}
    # A plan in the future keeps running; a lapsed one waits for the user.
    assert by_url["http://x/a"].status == STATUS_SCHEDULED
    assert by_url["http://x/a"].scheduled_at == future
    assert by_url["http://x/b"].status == STATUS_PAUSED
    assert by_url["http://x/b"].scheduled_at is None
    # The retry timer from the previous process is gone: surface the failure.
    assert by_url["http://x/c"].status == STATUS_FAILED
    assert by_url["http://x/c"].next_retry_at is None
    assert by_url["http://x/c"].auto_retries == 2


def test_task_roundtrips_scheduler_fields():
    task = DownloadTask(
        url="http://x/a",
        output="a",
        auto_retries=2,
        next_retry_at=1234.5,
        scheduled_at=9999.0,
    )
    restored = DownloadTask.from_dict(task.to_dict())
    assert restored.auto_retries == 2
    assert restored.next_retry_at == 1234.5
    assert restored.scheduled_at == 9999.0
