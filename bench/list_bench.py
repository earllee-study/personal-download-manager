"""Large download-list benchmark (QAbstractTableModel + QTableView offscreen).

The GUI never materialises one widget per task: the table asks the model only
for the rows it is about to paint, so a 20 000-row list must stay responsive.
This script measures the paths that run while the queue is busy:

* building the model one task at a time (as the queue does),
* painting the visible part of the view,
* applying progress updates from workers (unsorted and while sorted),
* filtering / sorting with the proxy,
* removing rows,
* serialising the whole list for downloads.json.

Run from the project root:

    python bench/list_bench.py
    python bench/list_bench.py --sizes 1000,5000,20000 --repeats 3

Results are written to ``bench/results/list-<date>.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import tracemalloc
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtWidgets import QApplication, QTableView  # noqa: E402

from personal_download_manager.gui.model import (  # noqa: E402
    COL_PROGRESS,
    COL_STATUS,
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_PAUSED,
    DownloadTableModel,
    DownloadTask,
    TaskFilterProxy,
)
from personal_download_manager.gui.widgets import ProgressDelegate  # noqa: E402

MIB = 1024 * 1024


def make_tasks(count: int, seed: int = 42) -> list[DownloadTask]:
    rng = random.Random(seed)
    statuses = [STATUS_COMPLETED] * 70 + [STATUS_PAUSED] * 20 + [STATUS_DOWNLOADING] * 10
    tasks: list[DownloadTask] = []
    for index in range(count):
        status = rng.choice(statuses)
        total = rng.randrange(64 * 1024, 2048 * MIB)
        downloaded = total if status == STATUS_COMPLETED else int(total * rng.random())
        tasks.append(
            DownloadTask(
                url=(
                    f"https://cdn{index % 50}.example.com/files/"
                    f"archive-{index:06d}.bin?token=abcdef{index:06d}"
                ),
                output=f"D:/downloads/video/archive-{index:06d}.bin",
                status=status,
                total_size=total,
                downloaded=downloaded,
                speed=3.5 * MIB if status == STATUS_DOWNLOADING else 0.0,
                connections=8 if status == STATUS_DOWNLOADING else 0,
                segments=8 if status == STATUS_DOWNLOADING else 0,
            )
        )
    return tasks


def median_ms(samples: list[float]) -> float:
    return statistics.median(samples) * 1000.0


def measure_once(
    app: QApplication, size: int, *, updates: int = 200
) -> dict:
    tracemalloc.start()
    tasks = make_tasks(size)
    model = DownloadTableModel()
    started = time.perf_counter()
    for task in tasks:
        model.add_task(task)
    build_s = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    proxy = TaskFilterProxy()
    proxy.setSourceModel(model)
    proxy.sort(-1)  # keep insertion order until a header is clicked

    view = QTableView()
    view.setModel(proxy)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(34)
    view.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(view))
    view.resize(1180, 700)
    view.show()
    app.processEvents()

    paint_samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        pixmap = view.viewport().grab()
        paint_samples.append(time.perf_counter() - started)
        assert not pixmap.isNull()
    app.processEvents()

    rng = random.Random(7)
    ids = [task.id for task in tasks]

    def progress_updates(sample_ids: list[str]) -> float:
        started = time.perf_counter()
        for task_id in sample_ids:
            model.update_task(
                task_id,
                status=STATUS_DOWNLOADING,
                downloaded=rng.randrange(1, 4096) * 4096,
                total_size=128 * MIB,
                speed=4.0 * MIB,
                eta=30.0,
                connections=8,
            )
        return time.perf_counter() - started

    update_sample = rng.sample(ids, min(updates, len(ids)))
    unsorted_s = progress_updates(update_sample)
    app.processEvents()

    started = time.perf_counter()
    proxy.sort(COL_STATUS)
    app.processEvents()
    sort_s = time.perf_counter() - started

    sorted_s = progress_updates(rng.sample(ids, min(updates, len(ids))))
    app.processEvents()

    started = time.perf_counter()
    proxy.set_search("archive-000123")
    filtered = proxy.rowCount()
    app.processEvents()
    filter_s = time.perf_counter() - started

    started = time.perf_counter()
    proxy.set_search("")
    app.processEvents()
    clear_filter_s = time.perf_counter() - started

    remove_ids = set(ids[: min(100, len(ids))])
    started = time.perf_counter()
    model.remove_rows(remove_ids)  # the bulk-delete path used by the GUI
    remove_s = time.perf_counter() - started
    app.processEvents()

    started = time.perf_counter()
    payload = json.dumps(
        [task.to_dict() for task in model.tasks()],
        ensure_ascii=False,
        indent=2,
    )
    save_s = time.perf_counter() - started

    view.deleteLater()
    app.processEvents()

    return {
        "rows": size,
        "build_ms": build_s * 1000.0,
        "peak_mib": peak / MIB,
        "paint_ms": median_ms(paint_samples),
        "update_ms": unsorted_s * 1000.0,
        "sorted_update_ms": sorted_s * 1000.0,
        "sort_ms": sort_s * 1000.0,
        "filter_ms": filter_s * 1000.0,
        "clear_filter_ms": clear_filter_s * 1000.0,
        "filtered_rows": filtered,
        "remove100_ms": remove_s * 1000.0,
        "save_ms": save_s * 1000.0,
        "save_mib": len(payload.encode("utf-8")) / MIB,
        "updates": len(update_sample),
    }


def summarize(samples: list[dict]) -> dict:
    keys = [
        "build_ms",
        "peak_mib",
        "paint_ms",
        "update_ms",
        "sorted_update_ms",
        "sort_ms",
        "filter_ms",
        "clear_filter_ms",
        "remove100_ms",
        "save_ms",
        "save_mib",
    ]
    result = {
        key: round(statistics.median(sample[key] for sample in samples), 3)
        for key in keys
    }
    result["rows"] = samples[0]["rows"]
    result["filtered_rows"] = samples[0]["filtered_rows"]
    result["updates"] = samples[0]["updates"]
    return result


HEADER = (
    "   rows   build   paint  update  sorted  sort  filter  remove100  save"
)


def print_row(summary: dict) -> None:
    print(
        f"{summary['rows']:>7}"
        f"{summary['build_ms']:>8.1f}"
        f"{summary['paint_ms']:>8.1f}"
        f"{summary['update_ms']:>8.1f}"
        f"{summary['sorted_update_ms']:>8.1f}"
        f"{summary['sort_ms']:>6.1f}"
        f"{summary['filter_ms']:>8.1f}"
        f"{summary['remove100_ms']:>11.1f}"
        f"{summary['save_ms']:>6.1f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes", default="1000,5000,20000", help="comma-separated row counts"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--label", default="", help="extra label stored in the result file"
    )
    args = parser.parse_args(argv)
    sizes = [int(item) for item in args.sizes.split(",") if item.strip()]
    repeats = max(1, args.repeats)

    app = QApplication.instance() or QApplication([])
    print("ms unless noted; median of "
          f"{repeats} runs per size; offscreen Qt")
    print(HEADER + "      MB")
    results = []
    for size in sizes:
        runs = [measure_once(app, size) for _ in range(repeats)]
        summary = summarize(runs)
        results.append({"summary": summary, "runs": runs})
        print_row(summary)
        # Tasks are reused across repeats; each run builds a fresh model.
        print(
            f"{'':>7}  peak Python allocations: {summary['peak_mib']:.1f} MiB; "
            f"downloads.json {summary['save_mib']:.1f} MiB; "
            f"filter matched {summary['filtered_rows']} row(s)"
        )

    payload = {
        "date": date.today().isoformat(),
        "label": args.label,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "qt": __import__("PySide6").__version__,
        "rows": sizes,
        "repeats": repeats,
        "results": results,
    }
    out = ROOT / "bench" / "results" / f"list-{date.today().isoformat()}.json"
    if args.label:
        out = out.with_name(
            out.stem + "-" + args.label.replace(" ", "_") + out.suffix
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"results -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
