"""Download task model shared by the queue manager and the table view."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor

from ..headers import format_eta, format_size, format_speed
from ..schedule import parse_repeat, repeat_label
from .i18n import tr, translate_repeat

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_SCHEDULED = "scheduled"   # waiting for its planned start time
STATUS_RETRYING = "retrying"     # failed, waiting for an automatic retry

STATUS_LABELS = {
    STATUS_QUEUED: "等待中",
    STATUS_DOWNLOADING: "下载中",
    STATUS_PAUSED: "已暂停",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
    STATUS_SCHEDULED: "已计划",
    STATUS_RETRYING: "等待重试",
}

STATUS_ORDER = {status: index for index, status in enumerate(STATUS_LABELS)}

STATUS_COLORS = {
    STATUS_QUEUED: "#6b7280",
    STATUS_DOWNLOADING: "#2563eb",
    STATUS_PAUSED: "#d97706",
    STATUS_COMPLETED: "#059669",
    STATUS_FAILED: "#dc2626",
    STATUS_CANCELLED: "#9ca3af",
    STATUS_SCHEDULED: "#7c3aed",
    STATUS_RETRYING: "#ea580c",
}

HEADERS = [
    "文件名",
    "状态",
    "进度",
    "大小",
    "速度",
    "剩余",
    "连接",
    "队列",
    "URL",
    "保存位置",
]
(
    COL_NAME,
    COL_STATUS,
    COL_PROGRESS,
    COL_SIZE,
    COL_SPEED,
    COL_ETA,
    COL_CONNECTIONS,
    COL_QUEUE,
    COL_URL,
    COL_DIR,
) = range(10)

TaskRole = Qt.ItemDataRole.UserRole + 1
ProgressRole = Qt.ItemDataRole.UserRole + 2
SortRole = Qt.ItemDataRole.UserRole + 3

# Which columns a field update can change.  Progress arrives several times per
# second per active task; repainting only the affected cells keeps a 20k-row
# list responsive.  A status change can touch every column (speed/ETA/colour).
_ALL_COLUMNS = tuple(range(len(HEADERS)))
_FIELD_COLUMNS: dict[str, tuple[int, ...]] = {
    "downloaded": (COL_PROGRESS, COL_SIZE),
    "total_size": (COL_PROGRESS, COL_SIZE),
    "progress_fraction": (COL_PROGRESS,),
    "speed": (COL_SPEED,),
    "eta": (COL_ETA,),
    "connections": (COL_CONNECTIONS,),
    "segments": (COL_CONNECTIONS,),
    "queue": (COL_QUEUE,),
    "output": (COL_NAME, COL_DIR),
    "url": (COL_NAME, COL_URL),
    "scheduled_at": (COL_ETA,),
    "next_retry_at": (COL_ETA,),
    "auto_retries": (COL_STATUS,),
    "error": (COL_STATUS,),
    "status": _ALL_COLUMNS,
}


def sort_key(task: DownloadTask, column: int):
    """Comparable value for header sorting (mirrors the ``SortRole`` data)."""
    if column == COL_PROGRESS:
        return task.progress
    if column == COL_SIZE:
        return task.total_size or 0
    if column == COL_SPEED:
        return task.speed
    if column == COL_ETA:
        return task.eta if task.eta is not None else 10**12
    if column == COL_CONNECTIONS:
        return task.connections
    if column == COL_STATUS:
        return STATUS_ORDER.get(task.status, 99)
    return DownloadTableModel._display(task, column)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class DownloadTask:
    url: str
    output: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = STATUS_QUEUED
    total_size: int | None = None
    downloaded: int = 0
    speed: float = 0.0
    eta: float | None = None
    connections: int = 0
    segments: int = 0
    queue: str = "主队列"
    # Set when the total byte size is unknown (HLS): a 0..1 fraction reported
    # by the downloader.  Not persisted; the final size is known at completion.
    progress_fraction: float | None = None
    retries: int = 0
    error: str | None = None
    added_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    scheduled_at: float | None = None   # local epoch seconds for a planned start
    # Daily/weekly/interval recurrence (see personal_download_manager.schedule): {"kind": ...}
    repeat: dict[str, Any] | None = None
    auto_retries: int = 0               # queue-level retries already used
    next_retry_at: float | None = None  # local epoch seconds for the next attempt
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return Path(self.output)

    @property
    def filename(self) -> str:
        return self.path.name or self.url.rsplit("/", 1)[-1] or "download"

    @property
    def progress(self) -> float:
        if self.progress_fraction is not None:
            return min(100.0, max(0.0, self.progress_fraction * 100.0))
        if self.total_size and self.total_size > 0:
            return min(100.0, self.downloaded * 100.0 / self.total_size)
        return 100.0 if self.status == STATUS_COMPLETED else 0.0

    @property
    def is_capture(self) -> bool:
        """True for a finished browser (MSE) capture imported into the queue."""
        return self.options.get("capture") == "mse"

    @property
    def status_label(self) -> str:
        if self.is_capture and self.status == STATUS_COMPLETED:
            if self.options.get("archiving"):
                return tr("归档中")
            return tr("已捕获")
        return tr(STATUS_LABELS.get(self.status, self.status))

    @property
    def repeat_text(self) -> str:
        """Human description of the recurrence (empty when it does not repeat)."""
        return translate_repeat(repeat_label(self.repeat))

    @property
    def search_key(self) -> str:
        """Lower-case "filename URL" used by the search filter (cached)."""
        cached = self.__dict__.get("_search_key_cache")
        if cached is None:
            cached = f"{self.filename} {self.url}".lower()
            self.__dict__["_search_key_cache"] = cached
        return cached

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "output": self.output,
            "queue": self.queue,
            "status": self.status,
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "retries": self.retries,
            "error": self.error,
            "added_at": self.added_at,
            "completed_at": self.completed_at,
            "scheduled_at": self.scheduled_at,
            "repeat": self.repeat,
            "auto_retries": self.auto_retries,
            "next_retry_at": self.next_retry_at,
            "options": self.options,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadTask":
        status = str(data.get("status", STATUS_QUEUED))
        # A process exit leaves no live worker: show interrupted jobs as paused.
        if status == STATUS_DOWNLOADING:
            status = STATUS_PAUSED
        total = data.get("total_size")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            total = None
        options = data.get("options")
        try:
            repeat_rule = parse_repeat(data.get("repeat"))
            repeat = repeat_rule.to_dict() if repeat_rule is not None else None
        except ValueError:
            repeat = None
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            url=str(data.get("url", "")),
            output=str(data.get("output", "")),
            queue=str(data.get("queue") or "主队列"),
            status=status,
            total_size=total,
            downloaded=_to_int(data.get("downloaded")),
            retries=_to_int(data.get("retries")),
            error=str(data["error"]) if data.get("error") else None,
            added_at=_to_float(data.get("added_at"), time.time()),
            completed_at=_to_float(data.get("completed_at"), None),
            scheduled_at=_to_float(data.get("scheduled_at"), None),
            repeat=repeat,
            auto_retries=_to_int(data.get("auto_retries")),
            next_retry_at=_to_float(data.get("next_retry_at"), None),
            options=dict(options) if isinstance(options, dict) else {},
        )


class DownloadTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tasks: list[DownloadTask] = []
        # task id -> row.  Progress updates arrive several times per second
        # per active task; without this index each one scanned the whole list.
        self._row_by_id: dict[str, int] = {}

    # -- Qt model API ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._tasks)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return tr(HEADERS[section])
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._tasks)):
            return None
        task = self._tasks[index.row()]
        column = index.column()

        if role == TaskRole:
            return task
        if role == ProgressRole:
            return task.progress
        if role == SortRole:
            if column == COL_PROGRESS:
                return task.progress
            if column == COL_SIZE:
                return task.total_size or 0
            if column == COL_SPEED:
                return task.speed
            if column == COL_ETA:
                return task.eta if task.eta is not None else 10**12
            if column == COL_CONNECTIONS:
                return task.connections
            if column == COL_STATUS:
                return STATUS_ORDER.get(task.status, 99)
            return self._display(task, column)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(task, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_URL:
                return task.url
            if column == COL_DIR:
                return task.output
            if column == COL_STATUS:
                return self._status_tooltip(task)
            return None
        if role == Qt.ItemDataRole.ForegroundRole and column == COL_STATUS:
            return QBrush(QColor(STATUS_COLORS.get(task.status, "#374151")))
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column in (COL_STATUS, COL_PROGRESS, COL_CONNECTIONS, COL_QUEUE):
                return int(Qt.AlignmentFlag.AlignCenter)
            if column in (COL_SIZE, COL_SPEED, COL_ETA):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    @staticmethod
    def _display(task: DownloadTask, column: int):
        if column == COL_NAME:
            return task.filename
        if column == COL_STATUS:
            return task.status_label
        if column == COL_PROGRESS:
            return f"{task.progress:.1f}%"
        if column == COL_SIZE:
            if task.total_size is not None:
                return format_size(task.total_size)
            if task.downloaded > 0:
                return f"{format_size(task.downloaded)} / ?"
            return "?"
        if column == COL_SPEED:
            return format_speed(task.speed) if task.status == STATUS_DOWNLOADING else "-"
        if column == COL_ETA:
            if task.status == STATUS_DOWNLOADING:
                return format_eta(task.eta)
            if task.status == STATUS_SCHEDULED and task.scheduled_at:
                return time.strftime("%m-%d %H:%M", time.localtime(task.scheduled_at))
            if task.status == STATUS_RETRYING and task.next_retry_at:
                return time.strftime("%H:%M:%S", time.localtime(task.next_retry_at))
            return "-"
        if column == COL_CONNECTIONS:
            return f"{task.connections}/{task.segments}" if task.connections else "-"
        if column == COL_QUEUE:
            return task.queue
        if column == COL_URL:
            return task.url
        if column == COL_DIR:
            return str(task.path.parent)
        return None

    @staticmethod
    def _status_tooltip(task: DownloadTask) -> str | None:
        lines: list[str] = []
        if task.is_capture:
            tracks = task.options.get("mse_tracks") or []
            lines.append(tr("MSE 捕获（{} 个轨道）").format(len(tracks)))
            if task.options.get("mse_merged"):
                lines.append(tr("已用 ffmpeg 合并视频与音频"))
            elif task.options.get("mse_merge_error"):
                lines.append(
                    tr("未合并：{}").format(task.options["mse_merge_error"])
                )
            if task.options.get("archiving"):
                lines.append(tr("正在归档到下载目录…"))
            elif task.options.get("archive_error"):
                lines.append(
                    tr("归档失败：{}").format(task.options["archive_error"])
                )
        if task.status == STATUS_SCHEDULED and task.scheduled_at:
            lines.append(
                tr("计划开始：")
                + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.scheduled_at))
            )
        if task.repeat:
            label = translate_repeat(repeat_label(task.repeat))
            if label:
                lines.append(tr("重复：") + label)
        if task.status == STATUS_RETRYING and task.next_retry_at:
            lines.append(
                tr("自动重试：")
                + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.next_retry_at))
                + tr("（已自动重试 {} 次）").format(task.auto_retries)
            )
        if task.error:
            lines.append(task.error)
        return "\n".join(lines) or None

    # -- mutation -------------------------------------------------------
    def _row_of(self, task_id: str) -> int | None:
        row = self._row_by_id.get(task_id)
        if (
            row is not None
            and 0 <= row < len(self._tasks)
            and self._tasks[row].id == task_id
        ):
            return row
        # Self-heal after any path that bypassed the normal mutation API.
        self._row_by_id = {
            task.id: index for index, task in enumerate(self._tasks)
        }
        return self._row_by_id.get(task_id)

    def add_task(self, task: DownloadTask) -> None:
        row = len(self._tasks)
        self.beginInsertRows(QModelIndex(), row, row)
        self._tasks.append(task)
        self._row_by_id[task.id] = row
        self.endInsertRows()

    def update_task(self, task_id: str, **fields) -> None:
        row = self._row_of(task_id)
        if row is None:
            return
        task = self._tasks[row]
        columns: set[int] = set()
        for key, value in fields.items():
            if not hasattr(task, key):
                continue
            setattr(task, key, value)
            columns.update(_FIELD_COLUMNS.get(key, _ALL_COLUMNS))
            if key in ("url", "output"):
                task.__dict__.pop("_search_key_cache", None)
        if not columns:
            return
        left = min(columns)
        right = max(columns)
        self.dataChanged.emit(self.index(row, left), self.index(row, right))

    def remove_task(self, task_id: str) -> None:
        row = self._row_of(task_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._tasks[row]
        self.endRemoveRows()
        del self._row_by_id[task_id]
        for index in range(row, len(self._tasks)):
            self._row_by_id[self._tasks[index].id] = index

    def remove_rows(self, task_ids: set[str]) -> None:
        # Remove bottom-up so the remaining rows shift only below each cut.
        rows = sorted(
            (row for row in (self._row_of(task_id) for task_id in task_ids)
             if row is not None),
            reverse=True,
        )
        for row in rows:
            if 0 <= row < len(self._tasks):
                self.beginRemoveRows(QModelIndex(), row, row)
                removed = self._tasks.pop(row)
                self.endRemoveRows()
                self._row_by_id.pop(removed.id, None)
        self._row_by_id = {
            task.id: index for index, task in enumerate(self._tasks)
        }

    def task_at(self, row: int) -> DownloadTask | None:
        return self._tasks[row] if 0 <= row < len(self._tasks) else None

    def find(self, task_id: str) -> DownloadTask | None:
        row = self._row_of(task_id)
        return None if row is None else self._tasks[row]

    def tasks(self) -> list[DownloadTask]:
        return list(self._tasks)

    def clear_completed(self) -> None:
        self.remove_rows(
            {t.id for t in self._tasks if t.status == STATUS_COMPLETED}
        )

    def replace_all(self, tasks: list[DownloadTask]) -> None:
        self.beginResetModel()
        self._tasks = tasks
        self._row_by_id = {
            task.id: index for index, task in enumerate(tasks)
        }
        self.endResetModel()


class TaskFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._filter = "all"
        self._search = ""
        self._queue = ""
        self.setSortRole(SortRole)

    def set_status_filter(self, value: str) -> None:
        self._filter = value
        self._invalidate()

    def set_search(self, text: str) -> None:
        self._search = (text or "").strip().lower()
        self._invalidate()

    def set_queue(self, name: str) -> None:
        self._queue = (name or "").strip()
        self._invalidate()

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        # Comparing tasks directly avoids two QModelIndex data() round-trips
        # per comparison; sorting 20k rows otherwise spends ~1s in bindings.
        model = self.sourceModel()
        if isinstance(model, DownloadTableModel):
            first = model.task_at(left.row())
            second = model.task_at(right.row())
            if first is None or second is None:
                return False
            return sort_key(first, left.column()) < sort_key(second, right.column())
        return super().lessThan(left, right)

    def _invalidate(self) -> None:
        # Qt 6.12 adds invalidate(); older releases use the row-filter variant.
        if hasattr(self, "invalidate"):
            self.invalidate()
        elif hasattr(self, "invalidateRowsFilter"):
            self.invalidateRowsFilter()
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, DownloadTableModel):
            return True
        task = model.task_at(source_row)
        if task is None:
            return False
        if self._filter == "active" and task.status not in (
            STATUS_QUEUED,
            STATUS_DOWNLOADING,
            STATUS_PAUSED,
            STATUS_SCHEDULED,
            STATUS_RETRYING,
        ):
            return False
        if self._filter == "completed" and task.status != STATUS_COMPLETED:
            return False
        if self._filter == "failed" and task.status not in (
            STATUS_FAILED,
            STATUS_CANCELLED,
            STATUS_RETRYING,
        ):
            return False
        if self._filter == "queued" and task.status != STATUS_QUEUED:
            return False
        if self._queue and task.queue != self._queue:
            return False
        if self._search and self._search not in task.search_key:
            return False
        return True
