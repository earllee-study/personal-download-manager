"""Queue, scheduler and concurrency manager for the GUI.

All methods here are called from the Qt main thread.  Workers run the existing
thread-safe engine and communicate exclusively through queued signals.

Two independent policies live here:

* **Planned start** -- a task carrying ``scheduled_at`` is held in
  ``STATUS_SCHEDULED`` until its time arrives, then enters the normal queue.
* **Failure retry queue** -- a transient failure (timeout, 5xx, 429, dropped
  connection) is put back into ``STATUS_RETRYING`` with exponential backoff
  instead of staying dead.  Permanent failures (404, 401, checksum mismatch)
  go straight to ``STATUS_FAILED``.

A single one-second QTimer drives both; it is cheap and keeps every timed
transition in one place.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..schedule import parse_repeat
from ..shutdown import run_shutdown, should_shut_down, shutdown_command
from ..paths import capture_dir
from .archive import ArchiveWorker
from .capture import is_mse_media_path
from .i18n import tr
from .model import (
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
from .settings import AppSettings
from .worker import DownloadWorker

log = logging.getLogger(__name__)

_MISSING = object()


def retry_delay(base: float, attempt: int, cap: float = 3600.0) -> float:
    """Seconds to wait before the ``attempt``-th automatic retry (0-based).

    Exponential (base, 2*base, 4*base, ...) so a server that is down for a
    long time is not hammered, capped so a retry always eventually happens.
    """
    base = max(1.0, float(base))
    return min(float(cap), base * (2 ** max(0, int(attempt))))


class QueueManager(QObject):
    stats_changed = Signal(dict)
    state_changed = Signal()
    task_finished = Signal(str)
    # "Shut down when done" is a session toggle (never persisted on): it is
    # armed explicitly, shows a cancellable countdown, and fires once.
    shutdown_armed_changed = Signal(bool)
    shutdown_countdown = Signal(int)  # seconds left; 0 means stopped/not armed
    shutdown_fired = Signal(str)      # the command line that was run
    shutdown_failed = Signal(str)     # error message from the runner
    archive_finished = Signal(str, str)  # task id, archived path
    archive_failed = Signal(str, str)    # task id, error message

    def __init__(
        self,
        model: DownloadTableModel,
        settings: AppSettings,
        parent=None,
        governor=None,
        shutdown_runner=None,
        capture_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.model = model
        self.settings = settings
        # Shared per-host connection caps and the global speed limit.  New
        # downloads pick up a replacement governor after a settings change;
        # running downloads keep the one they started with.
        self.governor = governor
        # Root used to validate source-track deletion; the GUI passes its
        # (possibly overridden) capture directory.
        self.capture_root = Path(capture_root) if capture_root else capture_dir()
        self.workers: dict[str, DownloadWorker] = {}
        self._queue: list[str] = []
        self._intents: dict[str, str] = {}
        self._delete_on_stop: set[str] = set()
        # Schedule requested for a task whose worker is still shutting down:
        # (planned start or None, repeat rule or None).
        self._pending_schedule: dict[str, tuple[float | None, dict | None]] = {}
        # MSE archival (finished captures moved into the download folder).
        self._archive_workers: dict[str, ArchiveWorker] = {}
        # Tasks removed while an archive is running: task id -> delete files?
        self._archive_pending_removals: dict[str, bool] = {}

        # Injectable so tests (and any future installer integration) never
        # actually power the machine off.  The real runner uses subprocess
        # with an argument list.
        self._shutdown_runner = shutdown_runner or run_shutdown
        self._shutdown_armed = False
        self._shutdown_fired = False
        self._shutdown_remaining = 0
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(1000)
        self._shutdown_timer.timeout.connect(self._tick_shutdown)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_schedule)
        self._timer.start()

    # ------------------------------------------------------------------
    # public operations
    # ------------------------------------------------------------------
    def add_task(self, task: DownloadTask, start: bool = True) -> None:
        self._apply_queue(task)
        self._arm_repeat(task)
        self.model.add_task(task)
        if task.scheduled_at is not None and task.scheduled_at > time.time():
            self.model.update_task(task.id, status=STATUS_SCHEDULED)
        elif start:
            self.enqueue(task.id)
        else:
            self.model.update_task(task.id, status=STATUS_QUEUED)
        self._changed()

    def add_captured_task(self, task: DownloadTask) -> None:
        """Insert an already-finished capture (MSE) without starting a worker.

        The task keeps the status it was built with (``completed``), so it
        appears in the normal list and filters but never touches the network.
        """
        self._apply_queue(task)
        self.model.add_task(task)
        self._changed()

    # ------------------------------------------------------------------
    # MSE capture archival (background, off the GUI thread)
    # ------------------------------------------------------------------
    def archive_capture_task(self, task: DownloadTask) -> None:
        """Move a finished capture into the download folder in the background.

        The task is already visible as "已捕获"; while the copy runs its status
        label becomes "归档中".  On success the output/size are rewritten to
        the archived file; on failure the capture stays where it is and the
        task remains usable.
        """
        if not task.is_capture or task.status != STATUS_COMPLETED:
            return
        if task.id in self._archive_workers or not self.settings.mse_archive:
            return
        source = Path(str(task.options.get("archive_source") or task.output))
        task.options["archive_source"] = str(source)
        if not source.is_file():
            task.options["archiving"] = False
            task.options["archive_pending"] = False
            task.options["archive_error"] = tr("捕获文件不存在")
            self.model.update_task(task.id, status=STATUS_COMPLETED)
            self._changed()
            return

        category = self.settings.category_folder(task.filename)
        dest = Path(self.settings.download_dir)
        if category:
            dest = dest.joinpath(*category.split("/"))
        size = int(task.total_size or 0) or None
        digest = task.options.get("mse_primary_sha256")
        task.options["archiving"] = True
        task.options["archive_pending"] = True
        task.options["archive_error"] = None

        worker = ArchiveWorker(
            task.id,
            source,
            dest,
            task.filename,
            expected_size=size,
            sha256=str(digest) if digest else None,
            parent=self,
        )
        worker.archived.connect(self._on_archive_finished)
        worker.failed.connect(self._on_archive_failed)
        worker.cancelled.connect(self._on_archive_cancelled)
        worker.finished.connect(lambda tid=task.id: self._on_archive_worker_finished(tid))
        self._archive_workers[task.id] = worker
        self.model.update_task(task.id, status=STATUS_COMPLETED)
        worker.start()
        self._changed()

    def resume_capture_archives(self) -> None:
        """Retry archives that were interrupted by an application exit."""
        changed = False
        for task in self.model.tasks():
            if not task.is_capture or not task.options.get("archive_pending"):
                continue
            if not self.settings.mse_archive:
                task.options["archive_pending"] = False
                task.options["archiving"] = False
                changed = True
                continue
            self.archive_capture_task(task)
        if changed:
            self._changed()

    def capture_source_paths(self, task: DownloadTask) -> list[Path]:
        """Source tracks that a cleanup may remove, without touching outputs.

        Only files inside ``<capture>/mse`` are considered; every task's current
        output is protected so a primary (unarchived) capture is never removed.
        """
        if not task.is_capture or task.options.get("archiving"):
            return []
        protected = {str(item.path.resolve()) for item in self.model.tasks()}
        protected.add(str(task.path.resolve()))
        raw_paths: list[str] = []
        for item in task.options.get("mse_tracks") or []:
            if isinstance(item, dict) and item.get("path"):
                raw_paths.append(str(item["path"]))
        raw_paths.extend(str(item) for item in task.options.get("extra_paths") or [] if item)
        found: list[Path] = []
        seen: set[str] = set()
        for raw in raw_paths:
            try:
                path = Path(raw).resolve()
            except (OSError, ValueError):
                continue
            key = str(path)
            if key in seen or key in protected:
                continue
            if not is_mse_media_path(path, self.capture_root):
                continue
            seen.add(key)
            found.append(path)
        return found

    def cleanup_capture_sources(self, task: DownloadTask) -> tuple[int, int]:
        """Delete a capture's source tracks (keeps outputs). Returns (n, bytes)."""
        deleted = 0
        freed = 0
        for path in self.capture_source_paths(task):
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                log.warning("could not remove capture source %s", path, exc_info=True)
                continue
            deleted += 1
            freed += size
        if not deleted:
            return 0, 0

        def existing(raw) -> bool:
            try:
                return bool(raw) and Path(str(raw)).is_file()
            except OSError:
                return False

        task.options["extra_paths"] = [
            str(item)
            for item in task.options.get("extra_paths") or []
            if existing(item)
        ]
        tracks: list[dict] = []
        for item in task.options.get("mse_tracks") or []:
            if not isinstance(item, dict):
                continue
            if item.get("path") and not existing(item["path"]):
                continue
            tracks.append(item)
        task.options["mse_tracks"] = tracks
        task.options["sources_deleted"] = True
        task.options["sources_deleted_count"] = (
            int(task.options.get("sources_deleted_count") or 0) + deleted
        )
        task.options["sources_freed_bytes"] = (
            int(task.options.get("sources_freed_bytes") or 0) + freed
        )
        self.model.update_task(task.id, status=STATUS_COMPLETED)
        self._changed()
        return deleted, freed

    def _on_archive_finished(self, task_id: str, path: str, size: int) -> None:
        task = self.model.find(task_id)
        if task is not None:
            removing = task_id in self._archive_pending_removals
            task.options["archiving"] = False
            task.options["archive_pending"] = False
            task.options["archive_error"] = None
            task.options["captured_path"] = task.options.get("archive_source")
            self.model.update_task(
                task_id,
                output=path,
                total_size=size,
                downloaded=size,
                status=STATUS_COMPLETED,
            )
            self.archive_finished.emit(task_id, path)
            if removing:
                self._finish_archive_removal(task_id)
            elif self.settings.mse_delete_sources and task.options.get("mse_merged"):
                # The merged file is safely in the download folder; the two
                # original tracks are now redundant (opt-in, default off).
                self.cleanup_capture_sources(task)
        self._changed()

    def _on_archive_failed(self, task_id: str, message: str) -> None:
        task = self.model.find(task_id)
        if task is not None:
            task.options["archiving"] = False
            task.options["archive_pending"] = False
            task.options["archive_error"] = message
            self.model.update_task(task_id, status=STATUS_COMPLETED)
            if task_id not in self._archive_pending_removals:
                # Do not warn about a task the user is already deleting.
                self.archive_failed.emit(task_id, message)
            self._finish_archive_removal(task_id)
        self._changed()

    def _on_archive_cancelled(self, task_id: str) -> None:
        task = self.model.find(task_id)
        if task is not None:
            if task_id in self._archive_pending_removals:
                self._finish_archive_removal(task_id)
            else:
                # Keep archive_pending so the next start retries; the source
                # file is still in place.
                task.options["archiving"] = False
                self.model.update_task(task_id, status=STATUS_COMPLETED)
        self._changed()

    def _on_archive_worker_finished(self, task_id: str) -> None:
        worker = self._archive_workers.pop(task_id, None)
        if worker is not None:
            worker.deleteLater()
        # Defensive: a removal requested just before the worker stopped must
        # not leave a completed row behind.
        if task_id in self._archive_pending_removals:
            self._finish_archive_removal(task_id)
            self._changed()

    def _finish_archive_removal(self, task_id: str) -> None:
        delete = self._archive_pending_removals.pop(task_id, None)
        if delete is None:
            return
        task = self.model.find(task_id)
        if task is None:
            return
        if delete:
            self._delete_files(task)
        self.model.remove_task(task_id)

    def enqueue(self, task_id: str, *, reset_retries: bool = False) -> None:
        task = self.model.find(task_id)
        if task is None or task_id in self.workers:
            return
        task.error = None
        self.model.update_task(
            task_id,
            status=STATUS_QUEUED,
            error=None,
            speed=0.0,
            eta=None,
            scheduled_at=None,
            next_retry_at=None,
        )
        if reset_retries:
            self.model.update_task(task_id, auto_retries=0)
        if task_id not in self._queue:
            self._queue.append(task_id)
        self._schedule()
        self._changed()

    def resume(self, task_id: str) -> None:
        """Start now: a paused, failed or scheduled task joins the queue."""
        task = self.model.find(task_id)
        if task is None or task.status == STATUS_COMPLETED:
            return
        self._pending_schedule.pop(task_id, None)
        self.enqueue(task_id, reset_retries=True)

    def pause(self, task_id: str) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        self._pending_schedule.pop(task_id, None)
        worker = self.workers.get(task_id)
        if worker is not None:
            self._intents[task_id] = "pause"
            worker.cancel()
            return
        if task_id in self._queue:
            self._queue.remove(task_id)
        # Pausing also cancels a plan or a pending automatic retry; the user
        # asked for the task to hold still.
        self.model.update_task(
            task_id,
            status=STATUS_PAUSED,
            speed=0.0,
            eta=None,
            scheduled_at=None,
            next_retry_at=None,
        )
        self._changed()

    def schedule_task(
        self,
        task_id: str,
        when: float | None,
        repeat: dict | None = None,
    ) -> None:
        """Plan a start time (epoch seconds) and/or a recurring rule.

        ``repeat`` (see :mod:`personal_download_manager.schedule`) arms a daily/weekly/every-N-days plan; its
        next occurrence is used unless ``when`` is a future moment.  Passing
        neither clears the plan (and the recurrence).  Works on idle tasks
        immediately and on a running task once its worker has stopped; the
        requested plan is replayed in ``_settle_pause``.
        """
        task = self.model.find(task_id)
        if task is None or task.status == STATUS_COMPLETED:
            return
        if task_id in self._queue:
            self._queue.remove(task_id)
        worker = self.workers.get(task_id)
        if worker is not None:
            self._pending_schedule[task_id] = (when, repeat)
            self._intents[task_id] = "pause"
            worker.cancel()
            return
        self._apply_schedule(task_id, when, repeat)

    def remove(self, task_id: str, delete_files: bool = False) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        if task_id in self._archive_workers:
            # Let the archive thread stop before touching files it may be
            # copying; the row is removed from the result handler.
            self._archive_pending_removals[task_id] = delete_files
            self._archive_workers[task_id].cancel()
            return
        if task_id in self._queue:
            self._queue.remove(task_id)
        self._pending_schedule.pop(task_id, None)
        if delete_files:
            self._delete_on_stop.add(task_id)
        worker = self.workers.get(task_id)
        if worker is not None:
            self._intents[task_id] = "remove"
            worker.cancel()
            return
        if delete_files:
            self._delete_files(task)
        self.model.remove_task(task_id)
        self._changed()

    def remove_many(
        self, task_ids: list[str] | set[str], delete_files: bool = False
    ) -> None:
        """Remove several tasks with one model pass (bulk delete from the UI)."""
        idle: list[str] = []
        for task_id in task_ids:
            task = self.model.find(task_id)
            if task is None:
                continue
            if task_id in self._archive_workers:
                self._archive_pending_removals[task_id] = delete_files
                self._archive_workers[task_id].cancel()
                continue
            if task_id in self._queue:
                self._queue.remove(task_id)
            self._pending_schedule.pop(task_id, None)
            worker = self.workers.get(task_id)
            if worker is not None:
                self._intents[task_id] = "remove"
                if delete_files:
                    self._delete_on_stop.add(task_id)
                worker.cancel()
                continue
            if delete_files:
                self._delete_files(task)
            idle.append(task_id)
        if idle:
            self.model.remove_rows(set(idle))
        self._changed()

    def remove_completed(self, delete_files: bool = False) -> None:
        completed = [
            task.id
            for task in self.model.tasks()
            if task.status == STATUS_COMPLETED
        ]
        if completed:
            self.remove_many(completed, delete_files)

    def kick(self) -> None:
        """Re-evaluate the queues (e.g. after changing concurrency)."""
        self._schedule()
        self._changed()

    def move_task(self, task_id: str, queue: str) -> None:
        """Move a task to another queue; applies to scheduling immediately."""
        task = self.model.find(task_id)
        if task is None or task.status == STATUS_COMPLETED:
            return
        limits = self.settings.queue_limits()
        target = queue if queue in limits else "主队列"
        self.model.update_task(task_id, queue=target)
        self._schedule()
        self._changed()

    def _apply_queue(self, task: DownloadTask) -> None:
        if task.queue not in self.settings.queue_limits():
            task.queue = "主队列"

    @staticmethod
    def _canonical_repeat(value) -> dict | None:
        """Validate a stored repeat rule and return its canonical JSON form."""
        if not value:
            return None
        try:
            rule = parse_repeat(value)
        except ValueError:
            log.warning("ignoring invalid repeat rule %r", value)
            return None
        return rule.to_dict() if rule is not None else None

    def _arm_repeat(self, task: DownloadTask) -> None:
        """Canonicalise a repeat rule and give it a next start when it has none."""
        task.repeat = self._canonical_repeat(task.repeat)
        if task.repeat is None:
            return
        rule = parse_repeat(task.repeat)
        if rule is not None and (
            task.scheduled_at is None or task.scheduled_at <= time.time()
        ):
            task.scheduled_at = rule.next_after()

    def _active_by_queue(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task_id in list(self.workers):
            task = self.model.find(task_id)
            if task is None:
                continue
            counts[task.queue] = counts.get(task.queue, 0) + 1
        return counts

    def set_governor(self, governor) -> None:
        """Use a new host/global policy for downloads started from now on."""
        self.governor = governor
        self.kick()

    def pause_all(self) -> None:
        for task in self.model.tasks():
            if task.status in (
                STATUS_DOWNLOADING,
                STATUS_QUEUED,
                STATUS_SCHEDULED,
                STATUS_RETRYING,
            ):
                self.pause(task.id)

    def resume_all(self) -> None:
        for task in self.model.tasks():
            if task.status in (STATUS_PAUSED, STATUS_FAILED, STATUS_CANCELLED):
                self.enqueue(task.id, reset_retries=True)

    # ------------------------------------------------------------------
    # "shut down when done"
    # ------------------------------------------------------------------
    @property
    def shutdown_armed(self) -> bool:
        return self._shutdown_armed

    def arm_shutdown(self) -> None:
        """Shut down after the queue drains; armed for this session only.

        If the queue is already idle (and at least one task completed) the
        countdown starts immediately; otherwise it starts when the last worker
        finishes.  Adding a task or pausing one cancels a running countdown
        but keeps the feature armed.
        """
        if self._shutdown_armed:
            return
        self._shutdown_armed = True
        self._shutdown_fired = False
        self.shutdown_armed_changed.emit(True)
        self._evaluate_shutdown()

    def cancel_shutdown(self) -> None:
        """Disarm the feature and stop any running countdown."""
        was_active = self._shutdown_armed or self._shutdown_timer.isActive()
        self._shutdown_armed = False
        self._shutdown_fired = False
        self._stop_shutdown_timer()
        if was_active:
            self.shutdown_countdown.emit(0)
            self.shutdown_armed_changed.emit(False)

    def _stop_shutdown_timer(self) -> None:
        self._shutdown_timer.stop()
        self._shutdown_remaining = 0

    def _shutdown_blocked(self) -> bool:
        if self.workers or self._queue:
            return True
        # Imported MSE captures are finished files, not downloads: they must
        # neither satisfy nor delay "shut down when downloads finish".
        return not should_shut_down(
            task.status for task in self.model.tasks() if not task.is_capture
        )

    def _evaluate_shutdown(self) -> None:
        if not self._shutdown_armed or self._shutdown_fired:
            return
        if self._shutdown_blocked():
            if self._shutdown_timer.isActive():
                self._stop_shutdown_timer()
                self.shutdown_countdown.emit(0)
            return
        if self._shutdown_timer.isActive():
            return
        self._shutdown_remaining = max(1, int(self.settings.shutdown_delay))
        self._shutdown_timer.start()
        self.shutdown_countdown.emit(self._shutdown_remaining)

    def _tick_shutdown(self) -> None:
        if not self._shutdown_armed or self._shutdown_fired:
            self._stop_shutdown_timer()
            return
        # Re-check on every tick: a new task may have been added between two
        # state changes (or a scheduled job may have become due).
        if self._shutdown_blocked():
            self._stop_shutdown_timer()
            self.shutdown_countdown.emit(0)
            return
        self._shutdown_remaining -= 1
        if self._shutdown_remaining > 0:
            self.shutdown_countdown.emit(self._shutdown_remaining)
            return
        self._fire_shutdown()

    def _fire_shutdown(self) -> None:
        self._stop_shutdown_timer()
        self._shutdown_fired = True
        self._shutdown_armed = False
        command = shutdown_command()
        try:
            self._shutdown_runner(command)
        except Exception as exc:  # OSError, CalledProcessError, ...
            log.exception("the shutdown command failed")
            self._shutdown_fired = False
            self.shutdown_countdown.emit(0)
            self.shutdown_armed_changed.emit(False)
            self.shutdown_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        log.info("shutdown after completed downloads: %s", command)
        self.shutdown_countdown.emit(0)
        self.shutdown_armed_changed.emit(False)
        self.shutdown_fired.emit(" ".join(command))

    def shutdown(self) -> None:
        self._timer.stop()
        # Never leave a countdown running during application exit.
        self._stop_shutdown_timer()
        self._shutdown_armed = False
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        for worker in workers:
            worker.wait(4000)
        for worker in workers:
            if worker.isRunning():
                # Never destroy a live QThread: Qt aborts.  The worker keeps
                # its parent (the manager) and is reaped with it.
                log.warning("a worker did not stop within 4s at shutdown")
        self.workers.clear()

        # Archive threads copy files; cancel them and keep the source intact.
        # A task whose pending flag survives is retried on the next start.
        archivers = list(self._archive_workers.values())
        for worker in archivers:
            worker.cancel()
        for worker in archivers:
            worker.wait(3000)
        for worker in archivers:
            if worker.isRunning():
                log.warning("an archive worker did not stop within 3s")
        self._archive_workers = {
            task_id: worker
            for task_id, worker in self._archive_workers.items()
            if worker.isRunning()
        }

    # ------------------------------------------------------------------
    # scheduling
    # ------------------------------------------------------------------
    def _tick_schedule(self) -> None:
        """Move due planned starts and due retries into the queue."""
        now = time.time()
        due = [
            task.id
            for task in self.model.tasks()
            if (
                task.status == STATUS_SCHEDULED
                and task.scheduled_at is not None
                and task.scheduled_at <= now
            )
            or (
                task.status == STATUS_RETRYING
                and task.next_retry_at is not None
                and task.next_retry_at <= now
            )
        ]
        for task_id in due:
            self.enqueue(task_id)

    def _apply_schedule(
        self,
        task_id: str,
        when: float | None,
        repeat: dict | None = None,
    ) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        rule = None
        if repeat:
            repeat = self._canonical_repeat(repeat)
            rule = parse_repeat(repeat) if repeat else None
        if rule is not None:
            # A recurring plan is defined by its wall-clock rule; an explicit
            # future ``when`` still wins when the caller supplied one.
            if when is None or when <= time.time():
                when = rule.next_after()
            self.model.update_task(
                task_id,
                status=STATUS_SCHEDULED,
                scheduled_at=float(when),
                repeat=rule.to_dict(),
                next_retry_at=None,
                speed=0.0,
                eta=None,
            )
        elif when is None:
            # Clearing the plan also drops the recurrence: the user asked for
            # this task to stop on its own (a plain pause keeps the rule).
            if task.status == STATUS_SCHEDULED:
                self.model.update_task(
                    task_id,
                    status=STATUS_PAUSED,
                    scheduled_at=None,
                    repeat=None,
                    speed=0.0,
                    eta=None,
                )
            elif task.status == STATUS_RETRYING:
                self.model.update_task(
                    task_id,
                    status=STATUS_FAILED,
                    next_retry_at=None,
                    repeat=None,
                    speed=0.0,
                    eta=None,
                )
            else:
                # A paused/idle recurring task: dropping the plan drops the
                # recurrence too.
                self.model.update_task(
                    task_id, scheduled_at=None, repeat=None
                )
        else:
            self.model.update_task(
                task_id,
                status=STATUS_SCHEDULED,
                scheduled_at=float(when),
                repeat=None,
                next_retry_at=None,
                speed=0.0,
                eta=None,
            )
        self._changed()

    def _schedule(self) -> None:
        limits = self.settings.queue_limits()
        for task in self.model.tasks():
            if task.queue not in limits:
                self.model.update_task(task.id, queue="主队列")

        active = self._active_by_queue()
        for name, cap in limits.items():
            capacity = cap - active.get(name, 0)
            while capacity > 0:
                next_id = None
                for candidate in list(self._queue):
                    task = self.model.find(candidate)
                    if (
                        task is None
                        or task.status != STATUS_QUEUED
                        or candidate in self.workers
                    ):
                        self._queue.remove(candidate)
                        continue
                    if task.queue != name:
                        continue
                    next_id = candidate
                    self._queue.remove(candidate)
                    break
                if next_id is None:
                    break
                self._start(next_id)
                capacity -= 1

    def _start(self, task_id: str) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        worker = DownloadWorker(task, self.settings, self.governor, self)
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(lambda tid=task_id: self._on_worker_finished(tid))
        self.workers[task_id] = worker
        self.model.update_task(
            task_id,
            status=STATUS_DOWNLOADING,
            error=None,
            speed=0.0,
            eta=None,
            connections=0,
        )
        worker.start()

    def _on_worker_finished(self, task_id: str) -> None:
        worker = self.workers.pop(task_id, None)
        if worker is not None:
            worker.deleteLater()
        self._schedule()
        self._changed()

    # ------------------------------------------------------------------
    # worker signals
    # ------------------------------------------------------------------
    def _on_progress(self, task_id: str, payload: dict) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        self.model.update_task(
            task_id,
            downloaded=int(payload.get("downloaded", task.downloaded)),
            total_size=payload.get("total") or task.total_size,
            speed=float(payload.get("speed", 0.0)),
            eta=payload.get("eta"),
            connections=int(payload.get("connections", 0)),
            segments=int(payload.get("segments", 0)),
            progress_fraction=payload.get("fraction"),
            status=STATUS_DOWNLOADING,
        )
        self._emit_stats()

    def _on_succeeded(self, task_id: str, result: dict) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        if self._intents.get(task_id) == "remove":
            self._finish_removal(task_id)
            return
        self._intents.pop(task_id, None)
        self._pending_schedule.pop(task_id, None)
        self.model.update_task(
            task_id,
            status=STATUS_COMPLETED,
            output=str(result.get("path", task.output)),
            total_size=int(result.get("total_size", task.total_size or 0)),
            downloaded=int(result.get("downloaded", task.downloaded)),
            speed=0.0,
            eta=None,
            retries=int(result.get("retries", task.retries)),
            error=None,
            completed_at=time.time(),
            scheduled_at=None,
            next_retry_at=None,
            progress_fraction=None,
        )
        self.task_finished.emit(task_id)
        # A recurring task goes straight back to "planned" for its next run.
        self._reschedule_repeat(task_id)
        self._changed()

    def _reschedule_repeat(self, task_id: str, error: str | None = None) -> bool:
        """Re-arm a recurring task for its next occurrence; False when inert."""
        task = self.model.find(task_id)
        if task is None:
            return False
        rule = None
        if task.repeat:
            try:
                rule = parse_repeat(task.repeat)
            except ValueError:
                task.repeat = None
        if rule is None:
            return False
        fields: dict = dict(
            status=STATUS_SCHEDULED,
            scheduled_at=rule.next_after(),
            speed=0.0,
            eta=None,
            next_retry_at=None,
            downloaded=0,
            progress_fraction=None,
            completed_at=None,
            auto_retries=0,
        )
        if error is not None:
            fields["error"] = error
        self.model.update_task(task_id, **fields)
        log.info("task %s repeats at %s", task_id, rule.describe())
        return True

    def _on_failed(self, task_id: str, message: str, retryable: bool = False) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        intent = self._intents.pop(task_id, None)
        if intent == "remove":
            self._finish_removal(task_id)
            return
        if intent == "pause":
            # The user cancelled at the same moment the download failed; their
            # pause (and any requested plan) wins over the retry policy.
            self._settle_pause(task_id)
            self._changed()
            return
        self._pending_schedule.pop(task_id, None)

        budget = self.settings.auto_retry_max
        used = task.auto_retries
        if retryable and self.settings.auto_retry and used < budget:
            delay = retry_delay(self.settings.auto_retry_delay, used)
            self.model.update_task(
                task_id,
                status=STATUS_RETRYING,
                error=message,
                speed=0.0,
                eta=None,
                auto_retries=used + 1,
                next_retry_at=time.time() + delay,
            )
            log.info(
                "task %s will retry in %.0fs (%d/%d): %s",
                task_id,
                delay,
                used + 1,
                budget,
                message,
            )
        elif not self._reschedule_repeat(task_id, message):
            self.model.update_task(
                task_id,
                status=STATUS_FAILED,
                error=message,
                speed=0.0,
                eta=None,
                next_retry_at=None,
            )
        self._changed()

    def _on_cancelled(self, task_id: str) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        intent = self._intents.pop(task_id, "pause")
        if intent == "remove":
            self._pending_schedule.pop(task_id, None)
            self._finish_removal(task_id)
            return
        if intent == "pause":
            self._settle_pause(task_id)
        else:
            self.model.update_task(
                task_id,
                status=STATUS_CANCELLED,
                speed=0.0,
                eta=None,
                next_retry_at=None,
            )
        self._changed()

    def _settle_pause(self, task_id: str) -> None:
        """Apply a user pause (and any plan queued behind the running worker)."""
        self.model.update_task(
            task_id,
            status=STATUS_PAUSED,
            speed=0.0,
            eta=None,
            next_retry_at=None,
        )
        pending = self._pending_schedule.pop(task_id, _MISSING)
        if pending is not _MISSING:
            when, repeat = pending
            self._apply_schedule(task_id, when, repeat)

    def _finish_removal(self, task_id: str) -> None:
        task = self.model.find(task_id)
        if task is None:
            return
        if task_id in self._delete_on_stop:
            self._delete_files(task)
        self._delete_on_stop.discard(task_id)
        self._pending_schedule.pop(task_id, None)
        self.model.remove_task(task_id)
        self._changed()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _delete_files(self, task: DownloadTask) -> None:
        output = task.path
        paths = [
            output,
            Path(str(output) + ".pdm.part"),
            Path(str(output) + ".pdm.json"),
        ]
        # MSE captures can own several tracks (and the merged output); deleting
        # the task with files removes the whole capture.
        if task.is_capture:
            for extra in task.options.get("extra_paths") or []:
                if extra:
                    paths.append(Path(str(extra)))
            captured = task.options.get("captured_path")
            if captured:
                paths.append(Path(str(captured)))
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete %s", path, exc_info=True)

    def _changed(self) -> None:
        self._emit_stats()
        self.state_changed.emit()
        # Cheap unless armed: one pass over the task list on state changes.
        self._evaluate_shutdown()

    def _emit_stats(self) -> None:
        tasks = self.model.tasks()
        active = sum(1 for t in tasks if t.status == STATUS_DOWNLOADING)
        queued = sum(1 for t in tasks if t.status == STATUS_QUEUED)
        paused = sum(1 for t in tasks if t.status == STATUS_PAUSED)
        completed = sum(1 for t in tasks if t.status == STATUS_COMPLETED)
        failed = sum(1 for t in tasks if t.status == STATUS_FAILED)
        scheduled = sum(1 for t in tasks if t.status == STATUS_SCHEDULED)
        retrying = sum(1 for t in tasks if t.status == STATUS_RETRYING)
        speed = sum(t.speed for t in tasks if t.status == STATUS_DOWNLOADING)
        self.stats_changed.emit(
            {
                "active": active,
                "queued": queued,
                "paused": paused,
                "completed": completed,
                "failed": failed,
                "scheduled": scheduled,
                "retrying": retrying,
                "speed": speed,
                "total": len(tasks),
            }
        )

    def load_tasks(self, items: list[dict]) -> None:
        tasks: list[DownloadTask] = []
        now = time.time()
        limits = self.settings.queue_limits()
        for item in items:
            try:
                task = DownloadTask.from_dict(item)
            except Exception:
                log.warning("skipping malformed saved task", exc_info=True)
                continue
            if not task.url or not task.output:
                continue
            if task.status == STATUS_QUEUED:
                # A queued item from the previous process must not start until
                # the user asks; present it as paused instead of a phantom
                # queue entry.
                task.status = STATUS_PAUSED
            elif task.status == STATUS_SCHEDULED:
                # A plan that is still in the future is honoured; one that
                # expired while the app was closed waits for the user instead
                # of surprising them with a launch-time download.  A recurring
                # plan instead rolls forward to its next occurrence.
                task.repeat = self._canonical_repeat(task.repeat)
                rule = parse_repeat(task.repeat) if task.repeat else None
                if task.scheduled_at is None or task.scheduled_at <= now:
                    if rule is not None:
                        task.scheduled_at = rule.next_after(now)
                    else:
                        task.status = STATUS_PAUSED
                        task.scheduled_at = None
            elif task.status == STATUS_RETRYING:
                # The retry timer died with the previous process.
                task.status = STATUS_FAILED
                task.next_retry_at = None
            if task.queue not in limits:
                task.queue = "主队列"
            tasks.append(task)
        self.model.replace_all(tasks)
        self._emit_stats()

    def export_state(self) -> list[dict]:
        return [task.to_dict() for task in self.model.tasks()]
