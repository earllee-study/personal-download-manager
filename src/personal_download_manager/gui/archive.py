"""Move finished MSE captures into the normal download folder, off the GUI thread.

The native host writes captures under ``%APPDATA%\\PersonalDownloadManager\\capture\\mse``.
Keeping multi-hundred-megabyte files there is fine functionally, but the user
expects finished media in their download folder.  Moving on the GUI thread can
freeze the window while a cross-volume copy runs, so this module provides:

* ``archive_capture()`` -- a pure, testable file operation: same-volume
  ``os.replace`` when possible, otherwise a chunked copy to a ``.part`` file
  with size/SHA-256 verification and an atomic rename;
* ``ArchiveWorker`` -- a QThread wrapper with cancellation and Qt signals.

The source file is never deleted before the destination is complete.  A
cancelled or failed copy leaves the capture in place.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..headers import unique_path

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


class ArchiveCancelled(Exception):
    """The user removed the task or the application is shutting down."""


class ArchiveError(Exception):
    """The capture could not be moved or failed integrity verification."""


def _source_size(source: Path) -> int:
    try:
        return source.stat().st_size
    except OSError as exc:
        raise ArchiveError(f"cannot read capture: {exc}") from exc


def archive_capture(
    source: Path,
    dest_dir: Path,
    filename: str,
    *,
    expected_size: int | None = None,
    sha256: str | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Archive one capture file into ``dest_dir`` and return the final path.

    * Same volume: ``os.replace`` is atomic and instant.
    * Cross volume: copy to ``<name>.pdm-archive.part`` in chunks, verify the
      size (and SHA-256 when provided), then atomically rename and remove the
      source.  Cancellation checks run between chunks.
    """
    source = Path(source)
    dest_dir = Path(dest_dir)
    cancel = cancel_event or threading.Event()
    if not source.is_file():
        raise ArchiveError(f"capture file is missing: {source}")
    if cancel.is_set():
        raise ArchiveCancelled()
    size = _source_size(source)
    if expected_size is not None and size != int(expected_size):
        raise ArchiveError("capture size does not match the completion record")

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"cannot create the download folder: {exc}") from exc
    if source.parent.resolve() == dest_dir.resolve():
        # Already in the target folder (e.g. the download dir is the spool).
        return source
    target = unique_path(dest_dir / filename)

    try:
        os.replace(source, target)
    except OSError:
        # Cross-device, or a filesystem that refuses the rename.
        temp = target.with_name(target.name + ".pdm-archive.part")
        hasher = hashlib.sha256() if sha256 else None
        try:
            with open(source, "rb") as src, open(temp, "wb") as dst:
                while True:
                    if cancel.is_set():
                        raise ArchiveCancelled()
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    if hasher is not None:
                        hasher.update(block)
                dst.flush()
                os.fsync(dst.fileno())
            if temp.stat().st_size != size:
                raise ArchiveError("archived size does not match the capture")
            if hasher is not None and hasher.hexdigest() != sha256:
                raise ArchiveError("archived SHA-256 does not match the capture")
            if cancel.is_set():
                raise ArchiveCancelled()
            os.replace(temp, target)
            try:
                shutil.copystat(source, target)
            except OSError:
                pass
            source.unlink(missing_ok=True)
        except ArchiveCancelled:
            temp.unlink(missing_ok=True)
            raise
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    return target


class ArchiveWorker(QThread):
    """Run :func:`archive_capture` without blocking the Qt main thread."""

    archived = Signal(str, str, int)   # task id, final path, size
    failed = Signal(str, str)          # task id, error message
    cancelled = Signal(str)            # task id

    def __init__(
        self,
        task_id: str,
        source: Path,
        dest_dir: Path,
        filename: str,
        *,
        expected_size: int | None = None,
        sha256: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.task_id = task_id
        self.source = Path(source)
        self.dest_dir = Path(dest_dir)
        self.filename = filename
        self.expected_size = expected_size
        self.sha256 = sha256
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D401
        try:
            target = archive_capture(
                self.source,
                self.dest_dir,
                self.filename,
                expected_size=self.expected_size,
                sha256=self.sha256,
                cancel_event=self._cancel,
            )
        except ArchiveCancelled:
            self.cancelled.emit(self.task_id)
        except Exception as exc:
            log.warning("could not archive capture %s", self.source, exc_info=True)
            self.failed.emit(self.task_id, str(exc))
        else:
            size = target.stat().st_size if target.is_file() else 0
            self.archived.emit(self.task_id, str(target), int(size))
