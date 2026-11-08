"""Daily/weekly recurring plans: pure rules, queue behaviour and dialogs."""

from __future__ import annotations

import os
import time
from datetime import date, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QDate, QDateTime, QEventLoop, QTime, QTimer
from PySide6.QtWidgets import QApplication, QDateTimeEdit

from personal_download_manager.gui.dialogs import (
    AddDownloadDialog,
    RepeatEditor,
    ScheduleDialog,
)
from personal_download_manager.gui.manager import QueueManager
from personal_download_manager.gui.model import (
    STATUS_FAILED,
    STATUS_SCHEDULED,
    DownloadTableModel,
    DownloadTask,
)
from personal_download_manager.gui.settings import AppSettings
from personal_download_manager.gui.worker import DownloadWorker
from personal_download_manager.schedule import RepeatRule, parse_repeat, repeat_label
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


def at(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second).timestamp()


def as_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch)


# ----------------------------------------------------------------------
# pure rules
# ----------------------------------------------------------------------
def test_daily_rule_picks_the_next_matching_time():
    rule = parse_repeat({"kind": "daily", "time": "03:30"})
    assert rule is not None
    assert as_datetime(rule.next_after(at(2026, 1, 5, 10, 0))) == datetime(
        2026, 1, 6, 3, 30
    )
    assert as_datetime(rule.next_after(at(2026, 1, 5, 3, 0))) == datetime(
        2026, 1, 5, 3, 30
    )
    # Exactly on the boundary schedules the *next* occurrence, never a rerun.
    assert as_datetime(rule.next_after(at(2026, 1, 6, 3, 30))) == datetime(
        2026, 1, 7, 3, 30
    )
    assert rule.describe() == "每天 03:30"


def test_weekly_rule_skips_unselected_weekdays():
    # 2026-01-05 is a Monday; 2026-01-09 is that week's Friday.
    rule = parse_repeat(
        {"kind": "weekly", "time": "22:00", "weekdays": [0, 4]}
    )
    assert rule is not None
    assert as_datetime(rule.next_after(at(2026, 1, 5, 10, 0))) == datetime(
        2026, 1, 5, 22, 0
    )
    assert as_datetime(rule.next_after(at(2026, 1, 5, 23, 0))) == datetime(
        2026, 1, 9, 22, 0
    )
    assert as_datetime(rule.next_after(at(2026, 1, 9, 22, 0))) == datetime(
        2026, 1, 12, 22, 0
    )
    assert "周一" in rule.describe() and "周五" in rule.describe()


def test_interval_rule_counts_from_its_anchor():
    """Every N days: anchor + k * days at a wall-clock time."""
    rule = parse_repeat(
        {
            "kind": "interval",
            "time": "03:30",
            "days": 3,
            "anchor": "2026-01-01",
        }
    )
    assert rule is not None
    # First occurrence is the anchor itself while it is still ahead.
    assert as_datetime(rule.next_after(at(2026, 1, 1, 2, 0))) == datetime(
        2026, 1, 1, 3, 30
    )
    # Exactly on the boundary schedules the next cadence date, never a rerun.
    assert as_datetime(rule.next_after(at(2026, 1, 1, 3, 30))) == datetime(
        2026, 1, 4, 3, 30
    )
    # Anywhere between the cadence dates lands on the next one.
    assert as_datetime(rule.next_after(at(2026, 1, 2, 10, 0))) == datetime(
        2026, 1, 4, 3, 30
    )
    assert as_datetime(rule.next_after(at(2026, 1, 4, 3, 0))) == datetime(
        2026, 1, 4, 3, 30
    )
    # Several intervals later still aligns to the anchor (Jan 1 + 4 * 3 days).
    assert as_datetime(rule.next_after(at(2026, 1, 10, 12, 0))) == datetime(
        2026, 1, 13, 3, 30
    )
    assert rule.describe() == "每隔 3 天 03:30"

    # An anchor in the future delays the first run instead of firing today.
    future = parse_repeat(
        {
            "kind": "interval",
            "time": "08:00",
            "days": 2,
            "anchor": "2026-02-01",
        }
    )
    assert future is not None
    assert as_datetime(future.next_after(at(2026, 1, 5, 9, 0))) == datetime(
        2026, 2, 1, 8, 0
    )
    assert as_datetime(future.next_after(at(2026, 2, 1, 9, 0))) == datetime(
        2026, 2, 3, 8, 0
    )

    # N=1 behaves like the daily rule.
    every_day = parse_repeat(
        {"kind": "interval", "time": "03:30", "days": 1, "anchor": "2026-01-01"}
    )
    assert every_day is not None
    daily = parse_repeat({"kind": "daily", "time": "03:30"})
    assert every_day.next_after(at(2026, 3, 7, 12, 0)) == daily.next_after(  # type: ignore[union-attr]
        at(2026, 3, 7, 12, 0)
    )
    assert every_day.describe() == "每天 03:30"


def test_interval_rule_defaults_anchor_to_today():
    rule = parse_repeat({"kind": "interval", "time": "03:30", "days": 2})
    assert rule is not None
    assert rule.anchor == date.today().isoformat()
    assert rule.next_after() > time.time() - 1
    # A persisted canonical rule keeps its anchor instead of sliding forward.
    assert parse_repeat(rule.to_dict()) == rule


def test_repeat_validation_and_labels():
    with pytest.raises(ValueError):
        parse_repeat({"kind": "weekly", "time": "22:00", "weekdays": []})
    with pytest.raises(ValueError):
        parse_repeat({"kind": "daily", "time": "25:00"})
    with pytest.raises(ValueError):
        parse_repeat({"kind": "hourly", "time": "10:00"})
    with pytest.raises(ValueError):
        parse_repeat({"kind": "weekly", "time": "10:00", "weekdays": [7]})
    with pytest.raises(ValueError):
        parse_repeat({"kind": "interval", "time": "10:00"})  # missing days
    with pytest.raises(ValueError):
        parse_repeat({"kind": "interval", "time": "10:00", "days": 0})
    with pytest.raises(ValueError):
        parse_repeat({"kind": "interval", "time": "10:00", "days": -2})
    with pytest.raises(ValueError):
        parse_repeat({"kind": "interval", "time": "10:00", "days": 3651})
    with pytest.raises(ValueError):
        parse_repeat(
            {"kind": "interval", "time": "10:00", "days": 2, "anchor": "2026-13-01"}
        )
    assert parse_repeat(None) is None
    assert parse_repeat("") is None
    with pytest.raises(ValueError):
        parse_repeat({})

    # Daily rules ignore any weekday list.
    daily = parse_repeat({"kind": "daily", "time": "09:15", "weekdays": [3]})
    assert daily is not None and daily.weekdays == ()
    # Daily/weekly rules ignore interval-only fields (and keep their JSON shape).
    daily_with_days = parse_repeat(
        {"kind": "daily", "time": "09:15", "days": 4, "anchor": "2026-01-01"}
    )
    assert daily_with_days is not None
    assert daily_with_days.to_dict() == {"kind": "daily", "time": "09:15"}

    assert repeat_label({"kind": "daily", "time": "09:15"}) == "每天 09:15"
    assert repeat_label({"kind": "bogus"}) == ""
    assert repeat_label({"kind": "weekly", "time": "07:05", "weekdays": [2]}).startswith(
        "每周 周三"
    )
    assert (
        repeat_label(
            {"kind": "interval", "time": "06:00", "days": 2, "anchor": "2026-01-01"}
        )
        == "每隔 2 天 06:00"
    )


def test_repeat_rule_to_dict_roundtrip():
    rule = RepeatRule("weekly", 7, 5, (0, 2, 4))
    assert parse_repeat(rule.to_dict()) == rule
    assert rule.to_dict() == {
        "kind": "weekly",
        "time": "07:05",
        "weekdays": [0, 2, 4],
    }

    interval = RepeatRule("interval", 3, 30, days=3, anchor="2026-01-01")
    assert interval.to_dict() == {
        "kind": "interval",
        "time": "03:30",
        "days": 3,
        "anchor": "2026-01-01",
    }
    assert parse_repeat(interval.to_dict()) == interval


def test_task_persists_the_repeat_rule():
    task = DownloadTask(
        url="https://x/a",
        output="a",
        repeat={"kind": "weekly", "time": "07:05", "weekdays": [2]},
    )
    restored = DownloadTask.from_dict(task.to_dict())
    assert restored.repeat == {
        "kind": "weekly",
        "time": "07:05",
        "weekdays": [2],
    }
    assert restored.repeat_text == "每周 周三 07:05"

    interval = DownloadTask(
        url="https://x/b",
        output="b",
        repeat={
            "kind": "interval",
            "time": "06:00",
            "days": 5,
            "anchor": "2026-01-01",
        },
    )
    restored_interval = DownloadTask.from_dict(interval.to_dict())
    assert restored_interval.repeat == {
        "kind": "interval",
        "time": "06:00",
        "days": 5,
        "anchor": "2026-01-01",
    }
    assert restored_interval.repeat_text == "每隔 5 天 06:00"

    broken = DownloadTask.from_dict(
        {"url": "https://x/a", "output": "a", "repeat": {"kind": "weekly", "time": "x"}}
    )
    assert broken.repeat is None
    broken_interval = DownloadTask.from_dict(
        {
            "url": "https://x/a",
            "output": "a",
            "repeat": {"kind": "interval", "time": "06:00", "days": 0},
        }
    )
    assert broken_interval.repeat is None


# ----------------------------------------------------------------------
# queue behaviour
# ----------------------------------------------------------------------
def test_completed_repeat_task_is_rearmed(qapp, server, tmp_path):
    data = make_content(256 * 1024, seed=301)
    server.add("/repeat.bin", Route(content=data, etag='"repeat"'))
    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    manager = QueueManager(DownloadTableModel(), settings)
    task = DownloadTask(
        url=server.url("/repeat.bin"),
        output=str(tmp_path / "repeat.bin"),
        repeat={"kind": "daily", "time": "03:30"},
        options={"connections": 2, "min_segment": "64K", "min_split": "64K"},
    )
    manager.add_task(task)
    try:
        first = manager.model.find(task.id)
        assert first.status == STATUS_SCHEDULED
        assert first.scheduled_at > time.time()

        manager.resume(task.id)  # run now; the recurrence is kept
        assert manager.model.find(task.id).status != STATUS_SCHEDULED

        assert wait_until(
            lambda: (manager.model.find(task.id) or task).status == STATUS_SCHEDULED,
            20,
        )
        finished = manager.model.find(task.id)
        assert (tmp_path / "repeat.bin").read_bytes() == data
        assert finished.repeat == {"kind": "daily", "time": "03:30"}
        assert finished.scheduled_at > time.time()
        assert finished.downloaded == 0
        assert finished.auto_retries == 0
        assert finished.status_label == "已计划"
    finally:
        manager.shutdown()


def test_failed_repeat_task_is_rearmed(qapp, server, tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path), max_concurrent=2, auto_retry=False
    )
    manager = QueueManager(DownloadTableModel(), settings)
    task = DownloadTask(
        url=server.url("/never.bin"),  # 404
        output=str(tmp_path / "never.bin"),
        repeat={"kind": "daily", "time": "04:00"},
    )
    manager.add_task(task)
    try:
        manager.resume(task.id)
        assert wait_until(
            lambda: (manager.model.find(task.id) or task).status == STATUS_SCHEDULED,
            20,
        )
        finished = manager.model.find(task.id)
        assert finished.status == STATUS_SCHEDULED
        assert "404" in (finished.error or "")
        assert finished.scheduled_at > time.time()
        assert finished.repeat is not None
    finally:
        manager.shutdown()


def test_non_repeat_failure_still_fails(qapp, server, tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path), max_concurrent=2, auto_retry=False
    )
    manager = QueueManager(DownloadTableModel(), settings)
    task = DownloadTask(
        url=server.url("/missing-again.bin"), output=str(tmp_path / "m.bin")
    )
    manager.add_task(task)
    try:
        assert wait_until(
            lambda: (manager.model.find(task.id) or task).status == STATUS_FAILED, 20
        )
        assert manager.model.find(task.id).repeat is None
    finally:
        manager.shutdown()


def test_load_tasks_rolls_expired_repeats_forward(qapp, tmp_path):
    manager = QueueManager(
        DownloadTableModel(), AppSettings(download_dir=str(tmp_path))
    )
    now = time.time()
    manager.load_tasks(
        [
            {
                "url": "http://x/old",
                "output": str(tmp_path / "old"),
                "status": STATUS_SCHEDULED,
                "scheduled_at": now - 120,
                "repeat": {"kind": "daily", "time": "03:30"},
            },
            {
                "url": "http://x/future",
                "output": str(tmp_path / "future"),
                "status": STATUS_SCHEDULED,
                "scheduled_at": now + 120,
                "repeat": {"kind": "daily", "time": "03:30"},
            },
            {
                "url": "http://x/plain",
                "output": str(tmp_path / "plain"),
                "status": STATUS_SCHEDULED,
                "scheduled_at": now - 120,
            },
        ]
    )
    manager.shutdown()
    by_url = {task.url: task for task in manager.model.tasks()}
    assert by_url["http://x/old"].status == STATUS_SCHEDULED
    assert by_url["http://x/old"].scheduled_at > now
    assert by_url["http://x/future"].status == STATUS_SCHEDULED
    assert by_url["http://x/plain"].status != STATUS_SCHEDULED
    assert by_url["http://x/plain"].scheduled_at is None


def test_interval_task_is_scheduled_on_its_cadence(qapp, tmp_path):
    anchor = date.today().isoformat()
    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    manager = QueueManager(DownloadTableModel(), settings)
    now = time.time()
    task = DownloadTask(
        url="http://x/interval",
        output=str(tmp_path / "interval"),
        repeat={"kind": "interval", "time": "03:30", "days": 2, "anchor": anchor},
    )
    manager.add_task(task)
    manager.shutdown()
    scheduled = manager.model.find(task.id)
    assert scheduled.status == STATUS_SCHEDULED
    assert scheduled.repeat == {
        "kind": "interval",
        "time": "03:30",
        "days": 2,
        "anchor": anchor,
    }
    assert scheduled.scheduled_at > now
    assert scheduled.repeat_text == "每隔 2 天 03:30"


def test_load_tasks_rolls_expired_interval_forward(qapp, tmp_path):
    manager = QueueManager(
        DownloadTableModel(), AppSettings(download_dir=str(tmp_path))
    )
    now = time.time()
    anchor = "2026-01-01"
    manager.load_tasks(
        [
            {
                "url": "http://x/interval-old",
                "output": str(tmp_path / "interval-old"),
                "status": STATUS_SCHEDULED,
                "scheduled_at": now - 120,
                "repeat": {
                    "kind": "interval",
                    "time": "03:30",
                    "days": 7,
                    "anchor": anchor,
                },
            }
        ]
    )
    manager.shutdown()
    task = next(t for t in manager.model.tasks() if t.url == "http://x/interval-old")
    assert task.status == STATUS_SCHEDULED
    assert task.scheduled_at > now
    # The cadence still points at anchor + k * 7 days after the rollover.
    run_date = datetime.fromtimestamp(task.scheduled_at).date()
    assert (run_date - date.fromisoformat(anchor)).days % 7 == 0
    assert task.repeat == {
        "kind": "interval",
        "time": "03:30",
        "days": 7,
        "anchor": anchor,
    }


def test_recurring_worker_overwrites_its_previous_file(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    task = DownloadTask(
        url="http://example.test/file.bin",
        output=str(tmp_path / "file.bin"),
        repeat={"kind": "daily", "time": "03:30"},
    )
    worker = DownloadWorker(task, settings)
    try:
        assert worker._build_config().overwrite is True
    finally:
        worker.deleteLater()
        QApplication.processEvents()

    plain = DownloadWorker(
        DownloadTask(url="http://example.test/f", output=str(tmp_path / "f")),
        settings,
    )
    try:
        assert plain._build_config().overwrite is False
    finally:
        plain.deleteLater()
        QApplication.processEvents()


# ----------------------------------------------------------------------
# dialogs
# ----------------------------------------------------------------------
def _editor(qapp) -> tuple[RepeatEditor, QDateTimeEdit]:
    edit = QDateTimeEdit()
    edit.setTime(QTime(3, 30))
    return RepeatEditor(edit), edit


def test_repeat_editor_roundtrip(qapp):
    editor, _ = _editor(qapp)
    assert editor.value() is None

    editor.kind.setCurrentIndex(editor.kind.findData("daily"))
    assert editor.value() == {"kind": "daily", "time": "03:30"}

    editor.kind.setCurrentIndex(editor.kind.findData("weekly"))
    # Switching to weekly pre-selects the date's weekday.
    assert editor.value() is not None
    for box in editor.day_checks:
        box.setChecked(False)
    with pytest.raises(ValueError):
        editor.value()
    editor.day_checks[0].setChecked(True)
    editor.day_checks[4].setChecked(True)
    assert editor.value() == {
        "kind": "weekly",
        "time": "03:30",
        "weekdays": [0, 4],
    }

    editor.set_value({"kind": "weekly", "time": "22:00", "weekdays": [6]})
    assert editor.value() == {
        "kind": "weekly",
        "time": "22:00",
        "weekdays": [6],
    }
    editor.reset()
    assert editor.value() is None


def test_repeat_editor_interval_roundtrip(qapp):
    editor, edit = _editor(qapp)
    editor.kind.setCurrentIndex(editor.kind.findData("interval"))
    today = edit.dateTime().date().toString("yyyy-MM-dd")
    editor.days.setValue(3)
    assert not editor.days.isHidden()
    assert editor.value() == {
        "kind": "interval",
        "time": "03:30",
        "days": 3,
        "anchor": today,
    }

    editor.set_value(
        {"kind": "interval", "time": "22:00", "days": 5, "anchor": "2026-02-01"}
    )
    assert editor.days.value() == 5
    assert editor.value() == {
        "kind": "interval",
        "time": "22:00",
        "days": 5,
        "anchor": "2026-02-01",
    }
    # Editing the time keeps the cadence anchor instead of sliding it.
    editor.time_edit.setTime(QTime(6, 0))
    assert editor.value()["anchor"] == "2026-02-01"  # type: ignore[index]

    editor.reset()
    assert editor.value() is None


def test_add_dialog_returns_an_interval_plan(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)
    dialog = AddDownloadDialog(settings, None, "https://example.com/file.bin")
    dialog.schedule_edit.setDateTime(
        QDateTime(QDate(2027, 1, 1), QTime(3, 30))
    )
    dialog.repeat_editor.kind.setCurrentIndex(
        dialog.repeat_editor.kind.findData("interval")
    )
    dialog.repeat_editor.days.setValue(4)
    dialog.accept()
    assert dialog.result_task is not None
    task = dialog.result_task
    assert task.repeat == {
        "kind": "interval",
        "time": "03:30",
        "days": 4,
        "anchor": "2027-01-01",
    }
    assert task.scheduled_at is not None and task.scheduled_at > time.time()
    assert as_datetime(task.scheduled_at).hour == 3
    assert as_datetime(task.scheduled_at).minute == 30


def test_schedule_dialog_keeps_interval_anchor(qapp):
    dialog = ScheduleDialog(
        None,
        {"kind": "interval", "time": "22:00", "days": 3, "anchor": "2026-01-01"},
    )
    dialog.accept()
    assert dialog.choice == ScheduleDialog.SET
    assert dialog.repeat == {
        "kind": "interval",
        "time": "22:00",
        "days": 3,
        "anchor": "2026-01-01",
    }
    when = as_datetime(dialog.when)
    assert when.hour == 22 and when.minute == 0
    assert (when.date() - date(2026, 1, 1)).days % 3 == 0


def test_add_dialog_returns_a_repeat_plan(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)
    dialog = AddDownloadDialog(settings, None, "https://example.com/file.bin")
    dialog.schedule_edit.setTime(QTime(3, 30))
    dialog.repeat_editor.kind.setCurrentIndex(
        dialog.repeat_editor.kind.findData("daily")
    )
    assert dialog.schedule_check.isChecked()
    assert not dialog.start_check.isEnabled()
    dialog.accept()
    assert dialog.result_task is not None
    task = dialog.result_task
    assert task.repeat == {"kind": "daily", "time": "03:30"}
    assert task.scheduled_at is not None and task.scheduled_at > time.time()
    assert as_datetime(task.scheduled_at).hour == 3
    assert as_datetime(task.scheduled_at).minute == 30


def test_schedule_dialog_sets_and_clears_repeat(qapp):
    dialog = ScheduleDialog(
        None, {"kind": "weekly", "time": "22:00", "weekdays": [0]}
    )
    dialog.accept()
    assert dialog.choice == ScheduleDialog.SET
    assert dialog.repeat == {"kind": "weekly", "time": "22:00", "weekdays": [0]}
    assert as_datetime(dialog.when).hour == 22
    assert as_datetime(dialog.when).weekday() == 0

    cleared = ScheduleDialog(
        time.time() + 3600, {"kind": "daily", "time": "22:00"}
    )
    cleared._clear()
    assert cleared.choice == ScheduleDialog.CLEAR
    assert cleared.repeat is None
    assert cleared.when is None
