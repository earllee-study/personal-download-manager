"""Large download list: indexing, bulk edits and view virtualisation.

The benchmark in ``bench/list_bench.py`` produces the published numbers; these
tests guard the invariants behind them without depending on absolute timings,
except for one deliberately loose scaling check that fails loudly if a
progress update becomes O(rows) again.
"""

from __future__ import annotations

import os
import random
import statistics
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QTableView

from personal_download_manager.gui.model import (
    COL_PROGRESS,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_PAUSED,
    DownloadTableModel,
    DownloadTask,
    TaskFilterProxy,
)
from personal_download_manager.gui.widgets import ProgressDelegate

MIB = 1024 * 1024


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def make_tasks(count: int, seed: int = 11) -> list[DownloadTask]:
    rng = random.Random(seed)
    tasks: list[DownloadTask] = []
    for index in range(count):
        status = rng.choice(
            [STATUS_COMPLETED, STATUS_COMPLETED, STATUS_PAUSED, STATUS_DOWNLOADING]
        )
        total = rng.randrange(64 * 1024, 1024 * MIB)
        tasks.append(
            DownloadTask(
                url=f"https://cdn{index % 20}.example.com/f/file-{index:05d}.bin",
                output=f"D:/downloads/file-{index:05d}.bin",
                status=status,
                total_size=total,
                downloaded=total if status == STATUS_COMPLETED else 0,
                speed=2.0 * MIB if status == STATUS_DOWNLOADING else 0.0,
                connections=8 if status == STATUS_DOWNLOADING else 0,
            )
        )
    return tasks


def build(count: int) -> DownloadTableModel:
    model = DownloadTableModel()
    for task in make_tasks(count):
        model.add_task(task)
    return model


def test_index_stays_correct_after_bulk_mutations(qapp):
    model = build(20000)
    tasks = model.tasks()
    rng = random.Random(3)
    sample = rng.sample(tasks, 500)
    for task in sample:
        model.update_task(
            task.id,
            downloaded=1234,
            total_size=4096,
            speed=MIB,
            eta=5.0,
            connections=4,
        )
        found = model.find(task.id)
        assert found is task
        assert found.downloaded == 1234
        assert found.total_size == 4096

    removed = {task.id for task in tasks[:500]}
    model.remove_rows(removed)
    assert model.rowCount() == 19500
    for task in tasks[:500]:
        assert model.find(task.id) is None

    # Every surviving row is still reachable through both APIs.
    for row, task in enumerate(model.tasks()):
        assert model.task_at(row) is task
        assert model.find(task.id) is task


def test_proxy_filter_and_sort_on_large_model(qapp):
    model = build(20000)
    proxy = TaskFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(-1)
    assert proxy.rowCount() == 20000

    proxy.set_search("file-00042")
    assert proxy.rowCount() == 1
    proxy.set_search("cdn7.example.com")
    assert proxy.rowCount() == 1000
    proxy.set_search("")
    assert proxy.rowCount() == 20000

    proxy.set_status_filter("completed")
    completed = sum(1 for task in model.tasks() if task.status == STATUS_COMPLETED)
    assert proxy.rowCount() == completed
    proxy.set_status_filter("all")


def test_view_paints_only_visible_rows(qapp):
    model = build(20000)
    proxy = TaskFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(-1)
    view = QTableView()
    view.setModel(proxy)
    view.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(view))
    view.resize(1180, 700)
    view.show()
    QApplication.processEvents()
    try:
        started = time.perf_counter()
        pixmap = view.viewport().grab()
        elapsed = time.perf_counter() - started
        assert not pixmap.isNull()
        # Only ~18 rows are visible; painting all 20k rows would take far
        # longer than this (the whole point of the virtualised table view).
        assert elapsed < 2.0, elapsed
    finally:
        view.deleteLater()
        QApplication.processEvents()


def _update_many(model: DownloadTableModel, ids: list[str], rounds: int) -> float:
    started = time.perf_counter()
    for index in range(rounds):
        model.update_task(
            ids[index % len(ids)],
            downloaded=index * 4096,
            speed=4.0 * MIB,
            eta=10.0,
            connections=8,
        )
    return time.perf_counter() - started


def _median_update_time(model: DownloadTableModel, rounds: int = 1000) -> float:
    ids = [task.id for task in model.tasks()]
    return statistics.median(
        _update_many(model, ids, rounds) for _ in range(3)
    )


def test_progress_updates_do_not_scan_the_whole_list(qapp):
    small = build(1000)
    large = build(20000)
    small_time = _median_update_time(small)
    large_time = _median_update_time(large)

    # A per-update linear scan would make the 20k model ~15-20x slower.
    # Generous slack keeps this stable on slow machines while still failing
    # if the id -> row index is lost.
    assert large_time < small_time * 5 + 0.05, (
        f"20k rows: {large_time:.3f}s vs 1k rows: {small_time:.3f}s"
    )
    assert large_time < 3.0, large_time
