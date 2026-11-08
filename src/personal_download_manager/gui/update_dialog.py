"""User-initiated update checker / verified installer download dialog."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..headers import format_size
from .i18n import tr
from ..update import (
    UpdateInfo,
    check_update,
    default_update_dir,
    download_update,
    launch_installer,
)


class _CheckWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], UpdateInfo | None], parent=None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            self.done.emit(self._func())
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            self.failed.emit(str(exc))


class _DownloadWorker(QThread):
    progress = Signal(int, int)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, func, info: UpdateInfo, directory: Path, timeout: float, parent=None) -> None:
        super().__init__(parent)
        self._func = func
        self._info = info
        self._directory = directory
        self._timeout = timeout
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            path = self._func(
                self._info,
                self._directory,
                progress=lambda done, total: self.progress.emit(
                    int(done), int(total or 0)
                ),
                cancel_event=self.cancel_event,
                timeout=self._timeout,
            )
            self.done.emit(str(path))
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            self.failed.emit(str(exc))


class UpdateDialog(QDialog):
    """Check a manifest, download and verify an installer, then ask to run it."""

    def __init__(
        self,
        manifest_url: str,
        current_version: str = __version__,
        parent: QWidget | None = None,
        *,
        start: bool = True,
        check_func: Callable[[], UpdateInfo | None] | None = None,
        download_func: Callable | None = None,
        launch_func: Callable | None = None,
        download_dir: Path | str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(parent)
        self.manifest_url = manifest_url
        self.current_version = current_version
        self._check_func = check_func or (
            lambda: check_update(
                manifest_url, current_version=current_version, timeout=timeout
            )
        )
        self._download_func = download_func or download_update
        self._launch_func = launch_func or launch_installer
        self._directory = Path(download_dir) if download_dir else default_update_dir()
        self._timeout = timeout
        self._busy: str | None = None
        self._info: UpdateInfo | None = None
        self._downloaded: str | None = None
        self._pending_close = False
        self._check_worker: _CheckWorker | None = None
        self._download_worker: _DownloadWorker | None = None

        self.setWindowTitle("检查更新")
        self.setMinimumWidth(560)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(170)
        self.notes.setVisible(False)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setRange(0, 0)
        self.download_button = QPushButton("下载并安装")
        self.download_button.setEnabled(False)
        self.close_button = QPushButton("关闭")
        self.download_button.clicked.connect(self._download)
        self.close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.download_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.notes)
        layout.addWidget(self.progress)
        layout.addLayout(buttons)

        if start:
            self.start_check()
        else:
            self.status.setText(
                tr("当前版本 {}。").format(self.current_version)
            )

    # ------------------------------------------------------------------
    def start_check(self) -> None:
        if self._busy:
            return
        self._busy = "check"
        self.download_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.notes.setVisible(False)
        self.progress.setVisible(False)
        self.status.setText(
            tr("正在检查更新…（当前版本 {}）").format(self.current_version)
        )
        worker = _CheckWorker(self._check_func, self)
        self._check_worker = worker
        worker.done.connect(self._on_checked)
        worker.failed.connect(self._on_check_failed)
        worker.finished.connect(self._on_check_finished)
        worker.start()

    def _on_checked(self, info: UpdateInfo | None) -> None:
        self._info = info
        if info is None:
            self.status.setText(
                tr("当前已是最新版本（{}）。").format(self.current_version)
            )
            return
        self.status.setText(
            tr("发现新版本 {}（当前 {}）。").format(
                info.version, self.current_version
            )
        )
        details = []
        if info.published_at:
            details.append(tr("发布时间：{}").format(info.published_at))
        if info.size is not None:
            details.append(tr("安装包大小：{}").format(format_size(info.size)))
        if info.notes:
            details.append("")
            details.append(info.notes)
        self.notes.setPlainText("\n".join(details))
        self.notes.setVisible(bool(details))
        self.download_button.setEnabled(True)

    def _on_check_failed(self, message: str) -> None:
        self.status.setText(tr("检查更新失败：{}").format(message))

    def _on_check_finished(self) -> None:
        self._busy = None
        self.close_button.setEnabled(True)
        if self._check_worker is not None:
            self._check_worker.deleteLater()
            self._check_worker = None

    # ------------------------------------------------------------------
    def _download(self) -> None:
        if self._info is None or self._busy:
            return
        self._busy = "download"
        self.download_button.setEnabled(False)
        self.close_button.setText("取消")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText(tr("正在下载更新…"))
        worker = _DownloadWorker(
            self._download_func, self._info, self._directory, self._timeout, self
        )
        self._download_worker = worker
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_downloaded)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(self._on_download_finished)
        worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
            self.status.setText(
                tr("正在下载更新… {} / {}").format(
                    format_size(done), format_size(total)
                )
            )
        else:
            self.status.setText(
                tr("正在下载更新… {}").format(format_size(done))
            )

    def _on_downloaded(self, path: str) -> None:
        self._downloaded = path
        self.status.setText(tr("更新包已下载并通过 SHA-256 校验。"))
        answer = QMessageBox.question(
            self,
            tr("安装更新"),
            tr("更新包已保存到：\n{}\n\n现在运行安装程序吗？").format(path),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self._launch_func(Path(path))
            except Exception as exc:  # noqa: BLE001 - shown in the dialog
                QMessageBox.warning(self, tr("无法启动安装程序"), str(exc))
                self.status.setText(tr("安装包已保存：{}").format(path))
            else:
                self.status.setText(tr("安装程序已启动。"))
                self._pending_close = True

    def _on_download_failed(self, message: str) -> None:
        if "cancel" in message.lower() or "取消" in message:
            self.status.setText(tr("下载已取消。"))
        else:
            self.status.setText(tr("下载失败：{}").format(message))
        self.progress.setVisible(False)

    def _on_download_finished(self) -> None:
        self._busy = None
        self.close_button.setText("关闭")
        self.close_button.setEnabled(True)
        if self._downloaded is None:
            self.download_button.setEnabled(self._info is not None)
        if self._download_worker is not None:
            self._download_worker.deleteLater()
            self._download_worker = None
        if self._pending_close:
            self.accept()

    # ------------------------------------------------------------------
    def reject(self) -> None:  # noqa: D401
        if self._busy == "download" and self._download_worker is not None:
            self._download_worker.cancel()
            self.close_button.setEnabled(False)
            self.status.setText(tr("正在取消下载…"))
            return
        if self._busy:
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            self.reject()
            event.ignore()
            return
        super().closeEvent(event)
