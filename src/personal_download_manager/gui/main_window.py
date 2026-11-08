"""Main window: toolbar, download table, queue, tray and persistence."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QSize, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..bandwidth import limit_at, parse_windows
from ..headers import (
    filename_from_url,
    format_size,
    format_speed,
    sanitize_filename,
    unique_path,
)
from ..hls import is_hls_url
from ..paths import capture_dir
from ..urlbatch import SUPPORTED_SCHEMES, is_http_url
from .capture import load_captures, load_mse_captures, make_mse_task, make_task
from .dialogs import AddDownloadDialog, BatchAddDialog, ScheduleDialog, SettingsDialog
from .i18n import install_translation_filter, tr, translate_widget_tree
from .manager import QueueManager
from .theme import install_ui_polish, polish_widget_tree
from .update_dialog import UpdateDialog
from .model import (
    COL_CONNECTIONS,
    COL_DIR,
    COL_ETA,
    COL_NAME,
    COL_PROGRESS,
    COL_QUEUE,
    COL_SIZE,
    COL_SPEED,
    COL_STATUS,
    COL_URL,
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
    TaskFilterProxy,
)
from .settings import (
    AppSettings,
    governor_from_settings,
    load_downloads,
    save_downloads,
)
from .theme import apply_theme
from .widgets import ProgressDelegate, make_app_icon, symbol_icon

log = logging.getLogger(__name__)


def _looks_like_url(text: str) -> bool:
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in SUPPORTED_SCHEMES and bool(parts.netloc)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings) -> None:
        super().__init__()
        self.settings = settings
        app = QApplication.instance()
        if app is not None:
            install_translation_filter(app)
            install_ui_polish(app)
        self.setWindowTitle("Personal Download Manager — 下载器")
        self.setWindowIcon(make_app_icon())
        self.resize(1180, 700)
        self.setMinimumSize(900, 520)

        self.model = DownloadTableModel(self)
        self.proxy = TaskFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.governor = governor_from_settings(self.settings)
        self.manager = QueueManager(
            self.model,
            self.settings,
            self,
            governor=self.governor,
            capture_root=capture_dir(),
        )
        self.manager.stats_changed.connect(self._on_stats)
        self.manager.state_changed.connect(self._schedule_save)
        self.manager.task_finished.connect(self._on_task_finished)
        self.manager.shutdown_armed_changed.connect(self._on_shutdown_armed)
        self.manager.shutdown_countdown.connect(self._on_shutdown_countdown)
        self.manager.shutdown_failed.connect(self._on_shutdown_failed)
        self.manager.archive_failed.connect(self._on_archive_failed)

        self._really_quit = False
        self._last_clipboard = ""
        self._dismissed_clipboard: set[str] = set()
        self._shutdown_seconds = 0
        self._shutdown_notice_shown = False
        self._shutdown_box: QMessageBox | None = None

        self._icon_actions: list[tuple[QAction, str]] = []
        self._build_ui()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self._build_tray()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)

        self._clip_timer = QTimer(self)
        self._clip_timer.setInterval(900)
        self._clip_timer.timeout.connect(self._check_clipboard)
        self._apply_clipboard_timer()

        self._capture_dir = capture_dir()
        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(1500)
        self._capture_timer.timeout.connect(self._poll_captures)

        self._bandwidth_timer = QTimer(self)
        self._bandwidth_timer.setInterval(30000)
        self._bandwidth_timer.timeout.connect(self._apply_bandwidth)
        self._bandwidth_timer.start()
        self._apply_bandwidth()

        # load_tasks emits stats_changed, so the status bar reflects restored
        # tasks immediately (they are presented as paused, never auto-started).
        self.manager.load_tasks(load_downloads())
        self.manager.resume_capture_archives()
        self._poll_captures()
        self._capture_timer.start()
        self._update_action_states()
        translate_widget_tree(self)
        polish_widget_tree(self)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_label = QLabel("显示")
        filter_label.setObjectName("SecondaryLabel")
        filter_row.addWidget(filter_label)
        self.filter_combo = QComboBox()
        for label, value in (
            ("全部", "all"),
            ("进行中", "active"),
            ("等待中", "queued"),
            ("已完成", "completed"),
            ("失败", "failed"),
        ):
            self.filter_combo.addItem(tr(label), value)
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        filter_row.addWidget(self.filter_combo)

        queue_label = QLabel("队列")
        queue_label.setObjectName("SecondaryLabel")
        filter_row.addWidget(queue_label)
        self.queue_combo = QComboBox()
        self._reload_queue_filter()
        self.queue_combo.currentIndexChanged.connect(self._queue_filter_changed)
        filter_row.addWidget(self.queue_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索文件名或 URL…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.proxy.set_search)
        filter_row.addWidget(self.search_edit, 1)

        self.summary_label = QLabel("共 0 个任务")
        self.summary_label.setObjectName("SecondaryLabel")
        filter_row.addWidget(self.summary_label)
        layout.addLayout(filter_row)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.proxy.sort(-1)  # keep insertion order until the user clicks a header
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self.table))
        self.table.doubleClicked.connect(lambda _index: self.open_selected_file())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_args: self._update_action_states()
        )
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_URL, QHeaderView.ResizeMode.Stretch)
        for column, width in (
            (COL_NAME, 230),
            (COL_STATUS, 82),
            (COL_PROGRESS, 132),
            (COL_SIZE, 92),
            (COL_SPEED, 102),
            (COL_ETA, 74),
            (COL_CONNECTIONS, 72),
            (COL_QUEUE, 90),
            (COL_DIR, 190),
        ):
            self.table.setColumnWidth(column, width)
        self.table_card = QFrame()
        self.table_card.setObjectName("ContentCard")
        card_layout = QVBoxLayout(self.table_card)
        card_layout.setContentsMargins(6, 6, 6, 6)
        card_layout.setSpacing(0)
        card_layout.addWidget(self.table)
        layout.addWidget(self.table_card, 1)
        self.setCentralWidget(central)
        self.search_edit.setFocus()

        self.setAcceptDrops(True)

    def _build_actions(self) -> None:
        self.add_action = self._action(
            "添加下载", self.add_download, "Ctrl+N", "add"
        )
        self.batch_action = self._action(
            "批量添加", self.add_batch, "Ctrl+Shift+N", "batch"
        )
        self.resume_action = self._action(
            "继续", self.resume_selected, "Ctrl+R", "play"
        )
        self.pause_action = self._action(
            "暂停", self.pause_selected, "Ctrl+P", "pause"
        )
        self.remove_action = self._action(
            "删除", self.remove_selected, "Del", "trash"
        )
        self.open_action = self._action(
            "打开文件", self.open_selected_file, "Ctrl+O", "file"
        )
        self.folder_action = self._action(
            "打开目录", self.open_selected_folder, "Ctrl+Shift+O", "folder"
        )
        self.schedule_action = self._action(
            "定时开始…", self.schedule_selected, "Ctrl+T", "clock"
        )
        self.settings_action = self._action(
            "设置", self.open_settings, "Ctrl+,", "sliders"
        )
        self.clear_action = self._action(
            "清除已完成", self.clear_completed, None, "check"
        )
        self.copy_action = self._action(
            "复制 URL", self.copy_selected_url, None, "copy"
        )
        self.properties_action = self._action(
            "属性", self.show_properties, None, "info"
        )
        self.cleanup_sources_action = self._action(
            "清理捕获源轨…", self.cleanup_capture_sources, None, None
        )
        self.quit_action = self._action("退出", self._quit, "Ctrl+Q")
        self.update_action = self._action(
            "检查更新…", self.check_updates, None, "refresh"
        )
        self.about_action = self._action("关于", self._about)
        self.shutdown_action = self._action(
            "所有下载完成后关机", self._toggle_shutdown, None, "check"
        )
        self.shutdown_action.setCheckable(True)
        self.shutdown_action.setToolTip(
            "队列空闲且所有任务都已结束（至少一个完成）后，倒计时结束即关机；"
            "倒计时期间可取消。此开关只对本次运行有效。"
        )

    def _action(
        self, text, slot, shortcut=None, symbol: str | None = None
    ) -> QAction:
        action = QAction(text, self)
        if symbol:
            action.setIcon(symbol_icon(symbol))
            self._icon_actions.append((action, symbol))
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        return action

    def _refresh_icons(self) -> None:
        for action, symbol in self._icon_actions:
            action.setIcon(symbol_icon(symbol))

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction(self.add_action)
        file_menu.addAction(self.batch_action)
        file_menu.addSeparator()
        file_menu.addAction(self.settings_action)
        file_menu.addSeparator()
        file_menu.addAction(self.quit_action)

        download_menu = self.menuBar().addMenu("下载")
        download_menu.addAction(self.resume_action)
        download_menu.addAction(self.pause_action)
        download_menu.addAction(self.remove_action)
        download_menu.addSeparator()
        download_menu.addAction(self.schedule_action)
        download_menu.addSeparator()
        download_menu.addAction(self.open_action)
        download_menu.addAction(self.folder_action)
        download_menu.addAction(self.copy_action)
        download_menu.addSeparator()
        download_menu.addAction(self.clear_action)
        download_menu.addAction(self.properties_action)
        download_menu.addAction(self.cleanup_sources_action)
        download_menu.addSeparator()
        pause_all = download_menu.addAction("全部暂停")
        pause_all.triggered.connect(self.manager.pause_all)
        resume_all = download_menu.addAction("全部继续")
        resume_all.triggered.connect(self.manager.resume_all)
        download_menu.addSeparator()
        download_menu.addAction(self.shutdown_action)

        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction(self.update_action)
        help_menu.addSeparator()
        help_menu.addAction(self.about_action)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        toolbar.addAction(self.add_action)
        toolbar.addAction(self.resume_action)
        toolbar.addAction(self.pause_action)
        toolbar.addAction(self.remove_action)
        toolbar.addSeparator()
        toolbar.addAction(self.folder_action)
        toolbar.addAction(self.settings_action)

    def _build_statusbar(self) -> None:
        self.status_left = QLabel("就绪")
        self.status_right = QLabel("0 B/s")
        self.status_left.setObjectName("SecondaryLabel")
        self.status_right.setObjectName("SecondaryLabel")
        self.shutdown_label = QLabel("")
        self.shutdown_label.setObjectName("SecondaryLabel")
        self.shutdown_label.hide()
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().addWidget(self.status_left, 1)
        self.statusBar().addPermanentWidget(self.shutdown_label)
        self.statusBar().addPermanentWidget(self.status_right)

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip("Personal Download Manager")
        menu = QMenu()
        show_action = menu.addAction("显示主窗口")
        show_action.triggered.connect(self._show_window)
        menu.addAction(self.add_action)
        menu.addSeparator()
        menu.addAction(self.quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    # ------------------------------------------------------------------
    # selection / actions
    # ------------------------------------------------------------------
    def _selected_ids(self) -> list[str]:
        selection = self.table.selectionModel()
        if selection is None:
            return []
        ids: list[str] = []
        for proxy_index in selection.selectedRows():
            source = self.proxy.mapToSource(proxy_index)
            task = self.model.task_at(source.row())
            if task is not None:
                ids.append(task.id)
        return ids

    def _selected_tasks(self) -> list[DownloadTask]:
        return [task for task in (self.model.find(i) for i in self._selected_ids()) if task]

    def _update_action_states(self) -> None:
        tasks = self._selected_tasks()
        statuses = {task.status for task in tasks}
        self.resume_action.setEnabled(
            bool(
                statuses
                & {
                    STATUS_PAUSED,
                    STATUS_FAILED,
                    STATUS_CANCELLED,
                    STATUS_QUEUED,
                    STATUS_SCHEDULED,
                    STATUS_RETRYING,
                }
            )
        )
        self.pause_action.setEnabled(
            bool(
                statuses
                & {
                    STATUS_DOWNLOADING,
                    STATUS_QUEUED,
                    STATUS_SCHEDULED,
                    STATUS_RETRYING,
                }
            )
        )
        self.remove_action.setEnabled(bool(tasks))
        self.open_action.setEnabled(bool(tasks))
        self.folder_action.setEnabled(bool(tasks))
        self.copy_action.setEnabled(bool(tasks))
        self.properties_action.setEnabled(bool(tasks))
        self.cleanup_sources_action.setEnabled(
            any(
                task.is_capture and self.manager.capture_source_paths(task)
                for task in tasks
            )
        )
        self.schedule_action.setEnabled(
            any(task.status != STATUS_COMPLETED for task in tasks)
        )
        self.clear_action.setEnabled(
            any(task.status == STATUS_COMPLETED for task in self.model.tasks())
        )

    # ------------------------------------------------------------------
    # add / queue operations
    # ------------------------------------------------------------------
    def add_download(self, initial_url: str = "") -> None:
        dialog = AddDownloadDialog(self.settings, self, initial_url)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_task:
            self.manager.add_task(dialog.result_task, start=dialog.start_now)
            self._save_now()
            self._show_window()

    def add_batch(self) -> None:
        dialog = BatchAddDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for url in dialog.urls:
            self._add_quick(
                url, base_dir=dialog.directory, category=dialog.category,
                start=dialog.start_now,
            )
        self._save_now()
        self._show_window()

    def _add_quick(
        self,
        url: str,
        base_dir: str | Path | None = None,
        category: str | None = None,
        start: bool = True,
    ) -> DownloadTask:
        base = Path(base_dir or self.settings.download_dir)
        spec = self.settings.default_category if category is None else category
        is_hls = is_http_url(url) and is_hls_url(url)
        name = sanitize_filename(filename_from_url(url) or "download")
        if is_hls and name.lower().endswith(".m3u8"):
            name = name[:-5] + ".ts"
        folder = self.settings.category_folder(name, spec)
        directory = base.joinpath(*folder.split("/")) if folder else base
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.warning("cannot create %s", directory, exc_info=True)
            directory = base
        output = directory / name
        if output.exists():
            output = unique_path(output)
        task = DownloadTask(
            url=url,
            output=str(output),
            options={
                "connections": self.settings.default_connections,
                "speed_limit": self.settings.speed_limit,
                "category": spec,
                "hls": is_hls,
                "hls_quality": "best" if is_hls else None,
            },
        )
        self.manager.add_task(task, start=start)
        return task

    def resume_selected(self) -> None:
        for task_id in self._selected_ids():
            self.manager.resume(task_id)

    def pause_selected(self) -> None:
        for task_id in self._selected_ids():
            self.manager.pause(task_id)

    def move_selected_to_queue(self, queue: str) -> None:
        for task_id in self._selected_ids():
            self.manager.move_task(task_id, queue)
        self._save_now()

    def schedule_selected(self) -> None:
        tasks = [t for t in self._selected_tasks() if t.status != STATUS_COMPLETED]
        if not tasks:
            return
        task = tasks[0]
        dialog = ScheduleDialog(task.scheduled_at, task.repeat, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        when = dialog.when if dialog.choice == ScheduleDialog.SET else None
        repeat = dialog.repeat if dialog.choice == ScheduleDialog.SET else None
        for item in tasks:
            self.manager.schedule_task(item.id, when, repeat)
        self._save_now()

    def remove_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        box = QMessageBox(self)
        box.setWindowTitle(tr("删除任务"))
        box.setText(tr("删除 {} 个任务？").format(len(ids)))
        box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        checkbox = QCheckBox(tr("同时删除已下载的文件和分片"))
        box.setCheckBox(checkbox)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.manager.remove_many(ids, delete_files=checkbox.isChecked())

    def clear_completed(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(tr("清除已完成"))
        box.setText(tr("从列表中移除所有已完成任务？"))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        checkbox = QCheckBox(tr("同时删除文件"))
        box.setCheckBox(checkbox)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.manager.remove_completed(delete_files=checkbox.isChecked())

    def cleanup_capture_sources(self) -> None:
        targets: list[tuple[DownloadTask, list[Path]]] = []
        for task in self._selected_tasks():
            paths = self.manager.capture_source_paths(task)
            if paths:
                targets.append((task, paths))
        if not targets:
            return
        count = sum(len(paths) for _, paths in targets)
        total = 0
        for _, paths in targets:
            for path in paths:
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        box = QMessageBox(self)
        box.setWindowTitle(tr("清理捕获源轨"))
        box.setText(
            tr("清理 {} 个捕获的 {} 个源轨（约 {}）？归档/主文件不会被删除。").format(
                len(targets), count, format_size(total)
            )
        )
        box.setIcon(QMessageBox.Icon.Question)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        deleted = 0
        freed = 0
        for task, _paths in targets:
            removed, bytes_freed = self.manager.cleanup_capture_sources(task)
            deleted += removed
            freed += bytes_freed
        if deleted:
            self._save_now()
            QMessageBox.information(
                self,
                tr("清理捕获源轨"),
                tr("已清理 {} 个文件，释放 {}。").format(
                    deleted, format_size(freed)
                ),
            )

    def copy_selected_url(self) -> None:
        tasks = self._selected_tasks()
        if tasks:
            QGuiApplication.clipboard().setText(tasks[0].url)

    def open_selected_file(self) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            return
        path = tasks[0].path
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.open_selected_folder()

    def open_selected_folder(self) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            return
        folder = tasks[0].path.parent
        if folder.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def show_properties(self) -> None:
        tasks = self._selected_tasks()
        if not tasks:
            return
        task = tasks[0]
        added = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.added_at))
        lines = [
            tr("文件名：{}").format(task.filename),
            tr("URL：{}").format(task.url),
            tr("保存到：{}").format(task.output),
            tr("状态：{}").format(task.status_label),
            tr("进度：{}").format(f"{task.progress:.1f}%"),
            tr("大小：{} 字节").format(task.total_size or "?"),
            tr("已下载：{} 字节").format(task.downloaded),
            tr("引擎重试：{}").format(task.retries),
            tr("自动重试：{}").format(task.auto_retries),
            tr("添加时间：{}").format(added),
        ]
        if task.is_capture:
            lines.append(tr("来源：MSE 捕获"))
            if task.options.get("title"):
                lines.append(tr("页面标题：{}").format(task.options["title"]))
            tracks = task.options.get("mse_tracks") or []
            lines.append(tr("捕获轨道：{} 个").format(len(tracks)))
            if task.options.get("mse_merged"):
                lines.append(tr("已用 ffmpeg 合并视频与音频"))
            elif task.options.get("mse_merge_error"):
                lines.append(
                    tr("未合并：{}").format(task.options["mse_merge_error"])
                )
            captured = task.options.get("captured_path")
            if captured and str(captured) != task.output:
                lines.append(tr("原始捕获：{}").format(captured))
            if task.options.get("archive_error"):
                lines.append(
                    tr("归档失败：{}").format(task.options["archive_error"])
                )
            if task.options.get("sources_deleted"):
                lines.append(
                    tr("源轨已清理：{} 个（{}）").format(
                        int(task.options.get("sources_deleted_count") or 0),
                        format_size(int(task.options.get("sources_freed_bytes") or 0)),
                    )
                )
            for extra in task.options.get("extra_paths") or []:
                lines.append(tr("附加文件：{}").format(extra))
        if task.scheduled_at:
            lines.append(
                tr("计划开始：{}").format(
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(task.scheduled_at)
                    )
                )
            )
        if task.repeat_text:
            lines.append(tr("重复：{}").format(task.repeat_text))
        if task.next_retry_at:
            lines.append(
                tr("下次自动重试：{}").format(
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(task.next_retry_at)
                    )
                )
            )
        if task.error:
            lines.append(tr("错误：{}").format(task.error))
        QMessageBox.information(self, tr("下载属性"), "\n".join(lines))

    # ------------------------------------------------------------------
    # context menu / settings / tray
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos) -> None:
        index = self.table.indexAt(pos)
        if index.isValid():
            self.table.selectRow(index.row())
        menu = QMenu(self.table)
        menu.addAction(self.resume_action)
        menu.addAction(self.pause_action)
        menu.addAction(self.remove_action)
        menu.addSeparator()
        menu.addAction(self.schedule_action)
        queue_menu = menu.addMenu("移动到队列")
        selected = self._selected_tasks()
        current = {task.queue for task in selected}
        queue_menu.setEnabled(bool(selected))
        for name in self.settings.queue_names():
            action = queue_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(current == {name})
            action.triggered.connect(
                lambda _checked=False, target=name: self.move_selected_to_queue(target)
            )
        menu.addSeparator()
        menu.addAction(self.open_action)
        menu.addAction(self.folder_action)
        menu.addAction(self.copy_action)
        menu.addSeparator()
        menu.addAction(self.properties_action)
        menu.addAction(self.cleanup_sources_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings.save()
        self.governor = governor_from_settings(self.settings)
        self.manager.set_governor(self.governor)
        self._reload_queue_filter()
        self._apply_bandwidth()
        self._apply_clipboard_timer()
        if self.tray is not None:
            self.tray.setVisible(
                self.settings.minimize_to_tray or self.settings.close_to_tray
            )
        apply_theme(QApplication.instance(), self.settings.theme)  # type: ignore[arg-type]
        self._refresh_icons()
        self._save_now()

    def check_updates(self) -> None:
        url = (self.settings.update_manifest or "").strip()
        if not url:
            QMessageBox.information(
                self,
                tr("检查更新"),
                tr(
                    "尚未配置更新清单 URL。\n\n"
                    "请在“设置 → 更新清单”中填写发布方提供的 HTTPS 地址。"
                ),
            )
            return
        UpdateDialog(url, __version__, self).exec()

    def _about(self) -> None:
        QMessageBox.about(
            self,
            tr("关于 Personal Download Manager"),
            tr(
                "Personal Download Manager {}\n\n"
                "多连接、动态分段、断点续传的 HTTP(S) 下载器。\n"
                "使用 deepseek-v4.1-flash-1017 开发的测试项目；"
                "下载引擎与测试均为本项目实现。\n\n"
                "请只下载你有权下载的内容。"
            ).format(__version__),
        )

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._really_quit = True
        self.close()
        QApplication.instance().quit()  # type: ignore[union-attr]

    def _on_task_finished(self, task_id: str) -> None:
        task = self.model.find(task_id)
        if task is not None and self.tray is not None:
            self.tray.showMessage(
                tr("下载完成"),
                task.filename,
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    # ------------------------------------------------------------------
    # "shut down when done"
    # ------------------------------------------------------------------
    def _toggle_shutdown(self, checked: bool) -> None:
        if checked:
            self.manager.arm_shutdown()
        else:
            self.manager.cancel_shutdown()

    def _on_shutdown_armed(self, armed: bool) -> None:
        self.shutdown_action.blockSignals(True)
        self.shutdown_action.setChecked(armed)
        self.shutdown_action.blockSignals(False)
        if not armed:
            self._shutdown_seconds = 0
            self._shutdown_notice_shown = False
            self._close_shutdown_box()
        self._render_shutdown()

    def _on_shutdown_countdown(self, seconds: int) -> None:
        self._shutdown_seconds = max(0, int(seconds))
        if self._shutdown_seconds <= 0:
            self._shutdown_notice_shown = False
            self._close_shutdown_box()
            self._render_shutdown()
            return
        self._render_shutdown()
        self._show_shutdown_box(self._shutdown_seconds)
        if not self._shutdown_notice_shown:
            self._shutdown_notice_shown = True
            if self.tray is not None:
                self.tray.showMessage(
                    tr("下载完成后关机"),
                    tr("下载队列已结束，将在 {} 秒后关闭计算机。").format(
                        self._shutdown_seconds
                    ),
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )

    def _render_shutdown(self) -> None:
        if self._shutdown_seconds > 0:
            text = tr("完成后关机：{} 秒").format(self._shutdown_seconds)
        elif self.manager.shutdown_armed:
            text = tr("完成后关机：已启用")
        else:
            self.shutdown_label.hide()
            return
        self.shutdown_label.setText(text)
        self.shutdown_label.show()

    def _show_shutdown_box(self, seconds: int) -> None:
        if self._shutdown_box is None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(tr("下载完成后关机"))
            box.setStandardButtons(QMessageBox.StandardButton.NoButton)
            cancel = box.addButton(
                tr("取消关机"), QMessageBox.ButtonRole.RejectRole
            )
            cancel.clicked.connect(self.manager.cancel_shutdown)
            box.rejected.connect(self.manager.cancel_shutdown)
            box.setModal(False)
            self._shutdown_box = box
        self._shutdown_box.setText(
            tr("下载队列已结束，将在 {} 秒后关闭计算机。").format(seconds)
        )
        if not self._shutdown_box.isVisible():
            self._shutdown_box.show()

    def _close_shutdown_box(self) -> None:
        box = self._shutdown_box
        self._shutdown_box = None
        if box is not None:
            # Closing the box must not be mistaken for a user cancellation
            # while the shutdown command is already being applied.
            box.blockSignals(True)
            box.close()
            box.deleteLater()

    def _on_shutdown_failed(self, message: str) -> None:
        self._shutdown_notice_shown = False
        self._render_shutdown()
        QMessageBox.critical(
            self,
            tr("关机命令执行失败"),
            tr("无法关闭计算机：\n{}").format(message),
        )

    def _on_archive_failed(self, task_id: str, message: str) -> None:
        task = self.model.find(task_id)
        name = task.filename if task is not None else task_id
        log.warning("MSE archive failed for %s: %s", name, message)
        if self.tray is not None:
            self.tray.showMessage(
                tr("MSE 归档失败"),
                tr("{}：{}").format(name, message),
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )

    # ------------------------------------------------------------------
    # browser captures
    # ------------------------------------------------------------------
    def _poll_captures(self) -> None:
        captures = load_captures(self._capture_dir)
        mse_captures = load_mse_captures(self._capture_dir)
        if not captures and not mse_captures:
            return
        added = 0
        if captures:
            busy = {
                task.url
                for task in self.model.tasks()
                if task.status
                in (
                    STATUS_QUEUED,
                    STATUS_DOWNLOADING,
                    STATUS_PAUSED,
                    STATUS_SCHEDULED,
                    STATUS_RETRYING,
                )
            }
            reserved: set[Path] = set()
            for path, capture in captures:
                try:
                    if capture.url in busy:
                        continue
                    task = make_task(
                        capture,
                        Path(self.settings.download_dir),
                        self.settings,
                        reserved,
                    )
                    reserved.add(task.path)
                    busy.add(capture.url)
                    self.manager.add_task(task, start=True)
                    added += 1
                finally:
                    path.unlink(missing_ok=True)

        # MSE captures are already finished files, not downloads: import each
        # session once as a completed queue entry.
        mse_added = 0
        known_sessions = {
            str(task.options.get("mse_session"))
            for task in self.model.tasks()
            if task.options.get("mse_session")
        }
        for path, capture in mse_captures:
            try:
                if capture.session in known_sessions:
                    continue
                task = make_mse_task(capture)
                self.manager.add_captured_task(task)
                if self.settings.mse_archive:
                    self.manager.archive_capture_task(task)
                known_sessions.add(capture.session)
                mse_added += 1
            except Exception:
                log.warning(
                    "could not import MSE capture %s", path, exc_info=True
                )
            finally:
                path.unlink(missing_ok=True)

        if added or mse_added:
            self._save_now()
            if self.tray is not None:
                if added:
                    self.tray.showMessage(
                        tr("浏览器下载"),
                        tr("已添加 {} 个任务").format(added),
                        QSystemTrayIcon.MessageIcon.Information,
                        3000,
                    )
                if mse_added:
                    self.tray.showMessage(
                        tr("MSE 捕获"),
                        tr("已导入 {} 个捕获").format(mse_added),
                        QSystemTrayIcon.MessageIcon.Information,
                        3000,
                    )
            self._show_window()

    # ------------------------------------------------------------------
    # filters / stats / persistence / clipboard
    # ------------------------------------------------------------------
    def _filter_changed(self) -> None:
        self.proxy.set_status_filter(str(self.filter_combo.currentData()))

    def _reload_queue_filter(self, selected: str | None = None) -> None:
        current = selected
        if current is None:
            current = str(self.queue_combo.currentData() or "")
        self.queue_combo.blockSignals(True)
        self.queue_combo.clear()
        self.queue_combo.addItem(tr("全部队列"), "")
        for name in self.settings.queue_names():
            self.queue_combo.addItem(name, name)
        index = self.queue_combo.findData(current)
        self.queue_combo.setCurrentIndex(index if index >= 0 else 0)
        self.queue_combo.blockSignals(False)
        self.proxy.set_queue(str(self.queue_combo.currentData() or ""))

    def _queue_filter_changed(self) -> None:
        self.proxy.set_queue(str(self.queue_combo.currentData() or ""))

    def _apply_bandwidth(self, now: datetime | None = None) -> None:
        windows = parse_windows(self.settings.bandwidth_windows)
        limit = limit_at(
            now or datetime.now(), windows, self.settings.global_speed_limit
        )
        if limit != self.governor.global_speed_limit:
            self.governor.set_global_speed_limit(limit)

    def _on_stats(self, stats: dict) -> None:
        text = tr(
            "下载中 {} · 等待 {} · 暂停 {} · 完成 {} · 失败 {}"
        ).format(
            stats["active"],
            stats["queued"],
            stats["paused"],
            stats["completed"],
            stats["failed"],
        )
        # Only mention the scheduler states when they are in use.
        if stats.get("scheduled"):
            text += tr(" · 计划 {}").format(stats["scheduled"])
        if stats.get("retrying"):
            text += tr(" · 重试 {}").format(stats["retrying"])
        self.status_left.setText(text)
        self.status_right.setText(format_speed(stats["speed"]))
        self.summary_label.setText(tr("共 {} 个任务").format(stats["total"]))
        self._update_action_states()

    def _schedule_save(self) -> None:
        self._save_timer.start(700)

    def _save_now(self) -> None:
        try:
            self.settings.save()
            save_downloads(self.manager.export_state())
        except OSError:
            log.warning("could not save GUI state", exc_info=True)

    def _apply_clipboard_timer(self) -> None:
        if self.settings.clipboard_monitor:
            self._clip_timer.start()
        else:
            self._clip_timer.stop()

    def _check_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text().strip()
        if text == self._last_clipboard:
            return
        self._last_clipboard = text
        if not self.settings.clipboard_monitor or not _looks_like_url(text):
            return
        if text in self._dismissed_clipboard:
            return
        if any(task.url == text for task in self.model.tasks()):
            return
        answer = QMessageBox.question(
            self,
            tr("剪贴板链接"),
            tr("发现下载链接：\n{}\n\n是否添加？").format(text),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.add_download(text)
        else:
            self._dismissed_clipboard.add(text)

    # ------------------------------------------------------------------
    # drag & drop / close / minimize
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls():
            urls = [url.toString() for url in mime.urls()]
        else:
            urls = mime.text().splitlines()
        urls = [url.strip() for url in urls if _looks_like_url(url.strip())]
        if not urls:
            return
        if len(urls) == 1:
            self.add_download(urls[0])
        else:
            for url in urls:
                self._add_quick(url)
        event.acceptProposedAction()

    def changeEvent(self, event) -> None:  # noqa: N802
        if (
            event.type() == QEvent.Type.WindowStateChange
            and self.isMinimized()
            and self.settings.minimize_to_tray
            and self.tray is not None
        ):
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if (
            not self._really_quit
            and self.settings.close_to_tray
            and self.tray is not None
        ):
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "Personal Download Manager",
                tr("程序仍在后台运行，双击托盘图标可恢复。"),
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return
        # Stop workers first, then deliver their queued result signals before
        # saving, so an archive that just finished is persisted with its new
        # path (and a cancelled one is retried next start).
        self.manager.shutdown()
        QApplication.processEvents()
        self._save_now()
        event.accept()
