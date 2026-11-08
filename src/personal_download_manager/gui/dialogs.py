"""Add-download and settings dialogs."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QDateTime, QTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..categories import (
    AUTO,
    NONE,
    folder_for,
    normalize_categories,
    parse_extensions,
)
from ..headers import (
    filename_from_url,
    sanitize_filename,
    validate_header,
)
from ..bandwidth import parse_hhmm
from ..dash import is_dash_url
from ..hls import is_hls_url
from ..models import DEFAULT_USER_AGENT
from ..policy import HostRule, normalize_proxy_groups
from ..schedule import (
    WEEKDAY_LABELS,
    WEEKDAY_SHORT,
    parse_repeat,
)
from ..urlbatch import (
    SUPPORTED_SCHEMES,
    UrlBatchError,
    expand_url_patterns,
    is_http_url,
    looks_like_url,
)
from .i18n import tr
from .model import DownloadTask
from .settings import AppSettings, parse_limit
from .theme import set_label_tone

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class RepeatEditor(QWidget):
    """Daily/weekly/every-N-days recurrence selector bound to a date picker.

    The date picker supplies the time-of-day.  For "every N days" its date also
    becomes the anchor of the cadence (the first occurrence), so choosing
    2026-11-01 with N=2 means Nov 1, Nov 3, Nov 5, ...  Editing an existing
    interval rule keeps its stored anchor; use "取消计划" to re-anchor.
    """

    def __init__(
        self, time_edit: QDateTimeEdit, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.time_edit = time_edit
        self._anchor = ""
        self.kind = QComboBox()
        self.kind.addItem(tr("不重复"), "none")
        self.kind.addItem(tr("每天"), "daily")
        self.kind.addItem(tr("每周"), "weekly")
        self.kind.addItem(tr("每隔 N 天"), "interval")
        self.days = QSpinBox()
        self.days.setRange(1, 365)
        self.days.setValue(2)
        self.days.setSuffix(tr(" 天"))
        self.days.setToolTip(tr("每隔 N 天在所选时间重复"))
        self.day_checks: list[QCheckBox] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("重复"))
        layout.addWidget(self.kind)
        layout.addWidget(self.days)
        for index, short in enumerate(WEEKDAY_SHORT):
            box = QCheckBox(short)
            box.setToolTip(WEEKDAY_LABELS[index])
            layout.addWidget(box)
            self.day_checks.append(box)
        layout.addStretch(1)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._show_days(False)
        self.days.setVisible(False)

    # ------------------------------------------------------------------
    def _show_days(self, visible: bool) -> None:
        for box in self.day_checks:
            box.setVisible(visible)

    def _kind_changed(self, *_args) -> None:
        kind = str(self.kind.currentData())
        weekly = kind == "weekly"
        self._show_days(weekly)
        self.days.setVisible(kind == "interval")
        if kind != "interval":
            self._anchor = ""
        if weekly and not any(box.isChecked() for box in self.day_checks):
            # Start from the weekday shown in the date picker; the user can
            # tick more days.
            weekday = self.time_edit.dateTime().date().dayOfWeek() - 1  # Mon=0
            self.day_checks[weekday].setChecked(True)

    def set_value(self, value: dict | None) -> None:
        rule = None
        if value:
            try:
                rule = parse_repeat(value)
            except ValueError:
                rule = None
        if rule is not None:
            self.time_edit.setTime(QTime(rule.hour, rule.minute))
        self.kind.blockSignals(True)
        index = self.kind.findData(rule.kind if rule is not None else "none")
        self.kind.setCurrentIndex(max(0, index))
        for day, box in enumerate(self.day_checks):
            box.setChecked(rule is not None and day in rule.weekdays)
        self._anchor = rule.anchor if rule is not None else ""
        if rule is not None and rule.kind == "interval":
            self.days.setValue(rule.days)
        self.kind.blockSignals(False)
        self._show_days(rule is not None and rule.kind == "weekly")
        self.days.setVisible(rule is not None and rule.kind == "interval")

    def value(self) -> dict | None:
        """Chosen rule as JSON, or ``None``; raises ValueError when invalid."""
        kind = str(self.kind.currentData() or "none")
        if kind == "none":
            return None
        moment = self.time_edit.dateTime().time()
        payload = {
            "kind": kind,
            "time": f"{moment.hour():02d}:{moment.minute():02d}",
        }
        if kind == "weekly":
            days = [
                index
                for index, box in enumerate(self.day_checks)
                if box.isChecked()
            ]
            if not days:
                raise ValueError(tr("每周重复至少选择一个星期。"))
            payload["weekdays"] = days
        elif kind == "interval":
            payload["days"] = self.days.value()
            payload["anchor"] = self._anchor or self.time_edit.dateTime().date().toString(
                "yyyy-MM-dd"
            )
        return parse_repeat(payload).to_dict()  # type: ignore[union-attr]

    def reset(self) -> None:
        self.kind.blockSignals(True)
        self.kind.setCurrentIndex(0)
        self.kind.blockSignals(False)
        self._anchor = ""
        self.days.setValue(2)
        self._show_days(False)
        self.days.setVisible(False)


class AddDownloadDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
        initial_url: str = "",
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.result_task: DownloadTask | None = None
        self.start_now = True
        self._filename_auto = True
        self.setWindowTitle("添加下载")
        self.setMinimumWidth(620)

        self.url_edit = QLineEdit(initial_url)
        self.url_edit.setPlaceholderText("https://example.com/file.zip")
        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空则使用服务器/URL 文件名")
        self.name_edit.textEdited.connect(self._name_edited)

        self.hls_check = QCheckBox("作为流媒体播放列表下载（HLS/DASH）")
        self.hls_check.setToolTip(
            "解析 m3u8 或 mpd，并发下载分片并按序拼接；支持未加密 VOD 与"
            "标准 AES-128（密钥可获取），SAMPLE-AES/DRM 拒绝"
        )
        self.hls_quality = QComboBox()
        self.hls_quality.addItem(tr("最佳码率"), "best")
        self.hls_quality.addItem(tr("最差码率"), "worst")
        self.hls_quality.setEnabled(False)
        self.dash_track = QComboBox()
        self.dash_track.addItem(tr("视频轨"), "video")
        self.dash_track.addItem(tr("音频轨"), "audio")
        self.dash_track.addItem(tr("视频+音频（合并）"), "both")
        self.dash_track.setToolTip(
            tr("合并需要设置 ffmpeg 路径（设置 → ffmpeg 路径）；没有 ffmpeg 时会保留两条轨道。")
        )
        self.dash_track.setVisible(False)
        self.dash_track.currentIndexChanged.connect(self._dash_track_changed)
        self.hls_check.toggled.connect(self.hls_quality.setEnabled)
        hls_row = QHBoxLayout()
        hls_row.addWidget(self.hls_check)
        hls_row.addWidget(self.hls_quality)
        hls_row.addWidget(self.dash_track)
        hls_row.addStretch(1)

        self.connections = QSpinBox()
        self.connections.setRange(1, 64)
        self.connections.setValue(settings.default_connections)
        self.connections.setSuffix(tr(" 连接"))

        self.limit_edit = QLineEdit()
        self.limit_edit.setPlaceholderText("不限，例如 5M")
        if settings.speed_limit:
            self.limit_edit.setText(f"{settings.speed_limit // 1024}K")

        self.category_combo = QComboBox()
        self.category_combo.addItem(tr("自动（按扩展名）"), AUTO)
        self.category_combo.addItem(tr("不分类"), NONE)
        for item in settings.categories:
            self.category_combo.addItem(
                f"{item['name']}（{item['folder']}）", item["name"]
            )
        index = self.category_combo.findData(settings.default_category)
        self.category_combo.setCurrentIndex(max(0, index))
        self.category_combo.setEnabled(settings.categories_enabled)
        self.category_hint = QLabel("")
        set_label_tone(self.category_hint, "secondary")
        category_row = QHBoxLayout()
        category_row.addWidget(self.category_combo, 1)
        category_row.addWidget(self.category_hint, 2)

        self.start_check = QCheckBox("立即开始下载")
        self.start_check.setChecked(True)

        self.schedule_check = QCheckBox("定时开始")
        self.schedule_edit = QDateTimeEdit(
            QDateTime.currentDateTime().addSecs(3600)
        )
        self.schedule_edit.setCalendarPopup(True)
        self.schedule_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.schedule_edit.setEnabled(False)
        self.repeat_editor = RepeatEditor(self.schedule_edit)
        self.schedule_check.toggled.connect(self._schedule_toggled)
        self.repeat_editor.kind.currentIndexChanged.connect(self._repeat_changed)
        schedule_row = QHBoxLayout()
        schedule_row.addWidget(self.schedule_check)
        schedule_row.addWidget(self.schedule_edit, 1)

        form = QFormLayout()
        form.addRow("下载地址", self.url_edit)
        form.addRow("保存目录", dir_row)
        form.addRow("文件名", self.name_edit)
        form.addRow("", hls_row)
        form.addRow("分类目录", category_row)
        form.addRow("连接数", self.connections)
        form.addRow("限速", self.limit_edit)
        form.addRow("", self.start_check)
        form.addRow("", schedule_row)
        form.addRow("", self.repeat_editor)

        self.advanced_toggle = QCheckBox("显示高级选项")
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.advanced_box = self._build_advanced()
        self.advanced_box.setVisible(False)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.advanced_toggle)
        layout.addWidget(self.advanced_box)
        layout.addStretch(1)
        layout.addWidget(buttons)

        self.url_edit.textChanged.connect(self._url_changed)
        self.name_edit.textChanged.connect(self._update_category_hint)
        self.category_combo.currentIndexChanged.connect(self._update_category_hint)
        if initial_url:
            self._url_changed(initial_url)
        self._update_category_hint()
        self.url_edit.setFocus()

    # ------------------------------------------------------------------
    def _build_advanced(self) -> QGroupBox:
        box = QGroupBox("高级选项")
        form = QFormLayout(box)
        self.referer_edit = QLineEdit()
        self.cookie_edit = QLineEdit()
        self.auth_edit = QLineEdit()
        self.auth_edit.setPlaceholderText("用户名:密码")
        self.proxy_edit = QLineEdit(self.settings.proxy or "")
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:8080 或 socks5://…")
        self.ua_edit = QLineEdit()
        self.ua_edit.setPlaceholderText("留空使用全局设置")
        self.sha_edit = QLineEdit()
        self.sha_edit.setPlaceholderText("可选，64 位十六进制")

        self.headers_edit = QPlainTextEdit()
        self.headers_edit.setPlaceholderText("每行一个请求头，例如：\nReferer: https://example.com/")
        self.headers_edit.setFixedHeight(70)

        self.mirrors_edit = QPlainTextEdit()
        self.mirrors_edit.setPlaceholderText(
            "可选，每行一个镜像 URL（内容/ETag 必须与主地址一致）"
        )
        self.mirrors_edit.setFixedHeight(56)

        self.retries = QSpinBox()
        self.retries.setRange(0, 20)
        self.retries.setValue(5)
        self.dynamic_check = QCheckBox("启用动态分段")
        self.dynamic_check.setChecked(True)
        self.adaptive_check = QCheckBox("自适应连接数（AIMD，实验性）")
        self.adaptive_check.setToolTip(
            "从少量连接开始，健康时逐步增加，遇到 429/503 时减半；上限为连接数"
        )
        self.http2_check = QCheckBox("尝试 HTTP/2（实验性）")
        self.http3_check = QCheckBox("尝试 HTTP/3（QUIC，实验性）")
        self.http3_check.setToolTip(
            "需要 aioquic；与 HTTP/2 互斥，且不支持代理"
        )
        self.http2_check.toggled.connect(self._http2_toggled)
        self.http3_check.toggled.connect(self._http3_toggled)
        self.verify_check = QCheckBox("校验 TLS 证书")
        self.verify_check.setChecked(self.settings.verify_tls)
        self.ftp_active_check = QCheckBox("FTP 主动模式（PORT/EPRT）")
        self.ftp_active_check.setToolTip(
            "仅 ftp:// 与 ftps://：由服务器连接回本机的数据端口；默认是被动模式"
        )
        self.sftp_key_edit = QLineEdit()
        self.sftp_key_edit.setPlaceholderText("可选，SFTP 私钥文件（如 id_ed25519）")
        self.sftp_key_pass_edit = QLineEdit()
        self.sftp_key_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.sftp_key_pass_edit.setPlaceholderText("私钥口令（可选）")
        self.sftp_auto_host_check = QCheckBox("首次连接时自动接受 SFTP 主机密钥")
        self.sftp_auto_host_check.setToolTip(
            "不安全：会接受任意主机密钥。默认拒绝未知主机；已知主机请用 "
            "--sftp-known-hosts 或系统 known_hosts。"
        )

        form.addRow("Referer", self.referer_edit)
        form.addRow("Cookie", self.cookie_edit)
        form.addRow("认证", self.auth_edit)
        form.addRow("代理", self.proxy_edit)
        form.addRow("User-Agent", self.ua_edit)
        form.addRow("SHA-256", self.sha_edit)
        form.addRow("额外请求头", self.headers_edit)
        form.addRow("镜像 URL", self.mirrors_edit)
        form.addRow("SFTP 私钥", self.sftp_key_edit)
        form.addRow("私钥口令", self.sftp_key_pass_edit)
        form.addRow("", self.sftp_auto_host_check)
        form.addRow("重试次数", self.retries)
        form.addRow("", self.dynamic_check)
        form.addRow("", self.adaptive_check)
        form.addRow("", self.http2_check)
        form.addRow("", self.http3_check)
        form.addRow("", self.verify_check)
        form.addRow("", self.ftp_active_check)
        return box

    def _http2_toggled(self, checked: bool) -> None:
        if checked and self.http3_check.isChecked():
            self.http3_check.setChecked(False)

    def _http3_toggled(self, checked: bool) -> None:
        if checked and self.http2_check.isChecked():
            self.http2_check.setChecked(False)

    def _toggle_advanced(self, visible: bool) -> None:
        self.advanced_box.setVisible(visible)
        self.adjustSize()

    def _schedule_toggled(self, enabled: bool) -> None:
        self.schedule_edit.setEnabled(enabled)
        # A planned start and "start now" are mutually exclusive.
        self.start_check.setEnabled(not enabled)
        if enabled:
            self.start_check.setChecked(False)
        else:
            self.start_check.setChecked(True)
            # Without a plan there is nothing to repeat.
            self.repeat_editor.reset()

    def _repeat_changed(self, *_args) -> None:
        if str(self.repeat_editor.kind.currentData()) != "none":
            if not self.schedule_check.isChecked():
                self.schedule_check.setChecked(True)
            self.schedule_edit.setEnabled(True)

    def _browse_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, tr("选择下载目录"), self.dir_edit.text() or self.settings.download_dir
        )
        if directory:
            self.dir_edit.setText(directory)

    def _name_edited(self, _text: str) -> None:
        self._filename_auto = False

    def _url_changed(self, text: str) -> None:
        url = text.strip()
        # HLS/DASH are HTTP-only; an ftp/sftp file named .m3u8 is a plain file.
        streaming = is_http_url(url)
        dash = streaming and is_dash_url(url)
        if streaming:
            if is_hls_url(url) or dash:
                self.hls_check.setChecked(True)
        elif self.hls_check.isChecked():
            self.hls_check.setChecked(False)
        self.dash_track.setVisible(dash)
        if not self._filename_auto:
            self._update_category_hint()
            return
        derived = filename_from_url(url) or ""
        if derived and streaming and is_hls_url(url) and derived.lower().endswith(".m3u8"):
            derived = derived[:-5] + ".ts"
        if derived and dash and derived.lower().endswith(".mpd"):
            suffix = ".m4a" if self.dash_track.currentData() == "audio" else ".mp4"
            derived = derived[:-4] + suffix
        self.name_edit.setText(sanitize_filename(derived) if derived else "")
        self._update_category_hint()

    def _dash_track_changed(self, *_args) -> None:
        """Keep an auto-filled DASH filename in step with the selected track."""
        if not self._filename_auto:
            return
        name = self.name_edit.text()
        if name.lower().endswith((".mp4", ".m4a")):
            suffix = ".m4a" if self.dash_track.currentData() == "audio" else ".mp4"
            self.name_edit.setText(name[:-4] + suffix)

    def _update_category_hint(self, *_args) -> None:
        if not self.settings.categories_enabled:
            self.category_hint.setText(tr("分类目录已在设置中关闭"))
            self.category_combo.setEnabled(False)
            return
        self.category_combo.setEnabled(True)
        name = self.name_edit.text().strip() or filename_from_url(
            self.url_edit.text().strip()
        ) or "download"
        folder = folder_for(
            name,
            str(self.category_combo.currentData() or NONE),
            self.settings.categories,
            True,
        )
        self.category_hint.setText(
            tr("子目录：{}").format(folder)
            if folder
            else tr("保存到下载目录根目录")
        )

    # ------------------------------------------------------------------
    def accept(self) -> None:  # noqa: D401
        url = self.url_edit.text().strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            self._warn(tr("下载地址格式不正确。"))
            return
        if parts.scheme.lower() not in SUPPORTED_SCHEMES or not parts.netloc:
            self._warn(
                tr("只支持 http://、https://、ftp://、ftps:// 或 sftp:// 地址。")
            )
            return

        directory = Path(self.dir_edit.text().strip() or self.settings.download_dir)
        streaming = self.hls_check.isChecked() and is_http_url(url)
        is_dash = streaming and is_dash_url(url)
        is_hls = streaming and not is_dash
        dash_track = str(self.dash_track.currentData() or "video")
        name = self.name_edit.text().strip()
        if not name:
            name = filename_from_url(url) or "download"
        name = sanitize_filename(name)
        if is_hls and name.lower().endswith(".m3u8"):
            name = name[:-5] + ".ts"
        if is_dash and name.lower().endswith(".mpd"):
            name = name[:-4] + (".m4a" if dash_track == "audio" else ".mp4")
        category_spec = str(self.category_combo.currentData() or NONE)
        folder = folder_for(
            name, category_spec, self.settings.categories,
            self.settings.categories_enabled,
        )
        if folder:
            directory = directory.joinpath(*folder.split("/"))
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._warn(tr("无法创建保存目录：{}").format(exc))
            return
        output = directory / name
        overwrite = False
        if output.exists():
            answer = QMessageBox.question(
                self,
                tr("文件已存在"),
                tr("{} 已存在。\n是否覆盖？").format(output.name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True

        try:
            limit = parse_limit(self.limit_edit.text())
        except (TypeError, ValueError):
            self._warn(tr("限速格式不正确，例如 5M、512K。"))
            return

        headers: dict[str, str] = {}
        for line in self.headers_edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" not in line:
                self._warn(tr("请求头格式不正确：{}").format(line))
                return
            key, value = line.split(":", 1)
            try:
                validate_header(key.strip(), value.strip())
            except ValueError as exc:
                self._warn(str(exc))
                return
            headers[key.strip()] = value.strip()

        auth = None
        if self.auth_edit.text().strip():
            if ":" not in self.auth_edit.text():
                self._warn(tr("认证格式应为 用户名:密码。"))
                return
            user, password = self.auth_edit.text().split(":", 1)
            auth = [user, password]

        sha = self.sha_edit.text().strip() or None
        if sha and not _SHA256_RE.match(sha):
            self._warn(tr("SHA-256 应为 64 位十六进制字符串。"))
            return

        mirrors = [
            line.strip()
            for line in self.mirrors_edit.toPlainText().splitlines()
            if line.strip()
        ]

        options = {
            "connections": self.connections.value(),
            "speed_limit": limit,
            "min_segment": "1M",
            "min_split": "1M",
            "dynamic": self.dynamic_check.isChecked(),
            "adaptive": self.adaptive_check.isChecked(),
            "hls": is_hls,
            "hls_quality": (
                str(self.hls_quality.currentData()) if streaming else None
            ),
            "dash": is_dash,
            "dash_track": dash_track if is_dash else None,
            "category": category_spec,
            "mirrors": mirrors,
            "retries": self.retries.value(),
            "referer": self.referer_edit.text().strip() or None,
            "cookie": self.cookie_edit.text().strip() or None,
            "auth": auth,
            "auth_type": "basic",
            "proxy": self.proxy_edit.text().strip() or None,
            "user_agent": self.ua_edit.text().strip() or None,
            "headers": headers,
            "sha256": sha,
            "http2": self.http2_check.isChecked(),
            "http3": self.http3_check.isChecked(),
            "ftp_active": self.ftp_active_check.isChecked(),
            "sftp_key": self.sftp_key_edit.text().strip() or None,
            "sftp_key_pass": self.sftp_key_pass_edit.text() or None,
            "sftp_auto_host_key": self.sftp_auto_host_check.isChecked(),
            "verify_tls": self.verify_check.isChecked(),
            "overwrite": overwrite,
        }
        try:
            repeat = self.repeat_editor.value()
        except ValueError as exc:
            self._warn(str(exc))
            return

        scheduled_at = None
        if repeat is not None:
            # The next matching occurrence is always in the future, so the
            # date shown in the picker is not used.
            scheduled_at = parse_repeat(repeat).next_after()  # type: ignore[union-attr]
        elif self.schedule_check.isChecked():
            scheduled_at = float(self.schedule_edit.dateTime().toSecsSinceEpoch())
            if scheduled_at <= time.time():
                self._warn(tr("计划开始时间必须晚于当前时间。"))
                return

        self.start_now = self.start_check.isChecked() and scheduled_at is None
        self.result_task = DownloadTask(
            url=url,
            output=str(output),
            options=options,
            scheduled_at=scheduled_at,
            repeat=repeat,
        )
        super().accept()

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, tr("输入有误"), text)


def _category_combo(settings: AppSettings, current: str) -> QComboBox:
    combo = QComboBox()
    combo.addItem(tr("自动（按扩展名）"), AUTO)
    combo.addItem(tr("不分类"), NONE)
    for item in settings.categories:
        combo.addItem(f"{item['name']}（{item['folder']}）", item["name"])
    index = combo.findData(current)
    combo.setCurrentIndex(max(0, index))
    combo.setEnabled(settings.categories_enabled)
    return combo


class BatchAddDialog(QDialog):
    """Add many URLs at once, with wildcard expansion."""

    LIMIT = 5000

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("批量添加（通配 URL）")
        self.setMinimumWidth(660)
        self.urls: list[str] = []
        self.directory = settings.download_dir
        self.category = settings.default_category
        self.start_now = True

        self.patterns_edit = QPlainTextEdit()
        self.patterns_edit.setPlaceholderText(
            "每行一个地址或模式，例如：\n"
            "https://cdn.example.com/img[01-10].jpg\n"
            "https://cdn.example.com/{a,b}/file.bin\n"
            "支持 [1-10]、[001-010]、[1-10:2]、[a-z]、{a,b,c}；# 开头为注释"
        )
        self.patterns_edit.setFixedHeight(150)

        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)

        self.category_combo = _category_combo(settings, settings.default_category)
        self.start_check = QCheckBox("立即开始下载")
        self.start_check.setChecked(True)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        set_label_tone(self.status_label, "secondary")

        form = QFormLayout()
        form.addRow("URL / 模式", self.patterns_edit)
        form.addRow("保存目录", dir_row)
        form.addRow("分类目录", self.category_combo)
        form.addRow("", self.start_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(buttons)

        self.patterns_edit.textChanged.connect(self._update_preview)
        self._update_preview()

    def _browse_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, tr("选择保存目录"), self.dir_edit.text() or self.settings.download_dir
        )
        if directory:
            self.dir_edit.setText(directory)

    def _expand(self) -> list[str] | None:
        try:
            urls = expand_url_patterns(
                self.patterns_edit.toPlainText(), self.LIMIT
            )
        except UrlBatchError as exc:
            self.status_label.setText(tr("模式有误：{}").format(exc))
            set_label_tone(self.status_label, "error")
            return None
        return urls

    def _update_preview(self) -> None:
        urls = self._expand()
        if urls is None:
            return
        if not urls:
            self.status_label.setText(tr("尚未输入任何 URL。"))
            set_label_tone(self.status_label, "secondary")
            return
        invalid = sum(1 for url in urls if not looks_like_url(url))
        text = tr("将添加 {} 个任务（上限 {}）。").format(len(urls), self.LIMIT)
        if len(urls) > 1:
            text += tr("，例如：{}").format(urls[0])
        if invalid:
            text += tr("；其中 {} 个不是受支持的地址，将被忽略").format(invalid)
            set_label_tone(self.status_label, "warning")
        else:
            set_label_tone(self.status_label, "secondary")
        self.status_label.setText(text)

    def accept(self) -> None:  # noqa: D401
        urls = self._expand()
        if urls is None:
            return
        urls = [url for url in urls if looks_like_url(url)]
        if not urls:
            QMessageBox.warning(
                self,
                tr("批量添加"),
                tr("没有识别到有效的地址（http/https/ftp/ftps/sftp）。"),
            )
            return
        directory = Path(self.dir_edit.text().strip() or self.settings.download_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("批量添加"), tr("无法创建保存目录：{}").format(exc)
            )
            return
        self.urls = urls
        self.directory = str(directory)
        self.category = str(self.category_combo.currentData() or NONE)
        self.start_now = self.start_check.isChecked()
        super().accept()


class CategoryDialog(QDialog):
    """Table editor for category folders and their extensions."""

    COLUMNS = ["名称", "子目录（相对下载目录）", "扩展名（逗号分隔）"]

    def __init__(
        self, categories: list[dict], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("分类目录")
        self.resize(760, 320)
        self.categories: list[dict] = normalize_categories(
            [dict(item) for item in categories if isinstance(item, dict)]
        )

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 120)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        for item in self.categories:
            self._add_row(item)

        add_button = QPushButton("添加")
        remove_button = QPushButton("删除选中")
        add_button.clicked.connect(lambda: self._add_row({}))
        remove_button.clicked.connect(self._remove_selected)
        edit_row = QHBoxLayout()
        edit_row.addWidget(add_button)
        edit_row.addWidget(remove_button)
        edit_row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "扩展名匹配时保存到“下载目录/子目录”。同一扩展名只归入第一个匹配"
                "的分类；留空的行会被忽略。"
            )
        )
        layout.addWidget(self.table, 1)
        layout.addLayout(edit_row)
        layout.addWidget(buttons)

    def _add_row(self, item: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            str(item.get("name", "") or ""),
            str(item.get("folder", "") or ""),
            ", ".join(parse_extensions(item.get("extensions"))),
        ]
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.setCurrentCell(row, 0)

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def accept(self) -> None:  # noqa: D401
        rows: list[dict] = []
        for row in range(self.table.rowCount()):
            name = self._cell(row, 0)
            folder = self._cell(row, 1)
            extensions = self._cell(row, 2)
            if not name:
                if folder or extensions:
                    QMessageBox.warning(
                        self,
                        tr("分类目录"),
                        tr("第 {} 行缺少名称。").format(row + 1),
                    )
                    return
                continue
            rows.append(
                {"name": name, "folder": folder or name, "extensions": extensions}
            )
        self.categories = normalize_categories(rows)
        super().accept()


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)

        self.dir_edit = QLineEdit(settings.download_dir)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)

        self.connections = QSpinBox()
        self.connections.setRange(1, 64)
        self.connections.setValue(settings.default_connections)

        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 10)
        self.concurrent.setValue(settings.max_concurrent)

        self._queues = [dict(item) for item in settings.queues]
        self.queue_button = QPushButton("管理队列…")
        self.queue_button.clicked.connect(self._edit_queues)
        concurrent_row = QHBoxLayout()
        concurrent_row.addWidget(self.concurrent, 1)
        concurrent_row.addWidget(self.queue_button)

        self.limit_edit = QLineEdit()
        self.limit_edit.setPlaceholderText("不限，例如 5M")
        if settings.speed_limit:
            self.limit_edit.setText(f"{settings.speed_limit // 1024}K")

        self._categories = [dict(item) for item in settings.categories]
        self.categories_check = QCheckBox("按分类保存到子目录")
        self.categories_check.setChecked(settings.categories_enabled)
        self.category_button = QPushButton("分类目录…")
        self.category_button.clicked.connect(self._edit_categories)
        self.default_category_combo = QComboBox()
        self._reload_category_combo(settings.default_category)
        category_row = QHBoxLayout()
        category_row.addWidget(self.default_category_combo, 1)
        category_row.addWidget(self.category_button)

        self.ua_edit = QLineEdit(settings.user_agent)
        self.proxy_edit = QLineEdit(settings.proxy or "")
        self.proxy_edit.setPlaceholderText("http://127.0.0.1:8080 或 socks5://…")
        self.update_manifest_edit = QLineEdit(settings.update_manifest or "")
        self.update_manifest_edit.setPlaceholderText(
            "https://…/latest.json（留空则不检查更新）"
        )
        self.update_manifest_edit.setToolTip(
            "仅在你点击“帮助 → 检查更新”时访问；安装包必须通过 SHA-256 校验，"
            "运行安装器前还会再次确认。"
        )
        self.ffmpeg_edit = QLineEdit(settings.ffmpeg_path or "")
        self.ffmpeg_edit.setPlaceholderText(
            tr("ffmpeg.exe 或包含 ffmpeg 的目录（留空则从 PATH 查找）")
        )
        self.ffmpeg_edit.setToolTip(
            tr("用于把 DASH 视频轨和音频轨合并为可播放 MP4；留空则从 PATH 查找。")
        )

        self.global_limit_edit = QLineEdit()
        self.global_limit_edit.setPlaceholderText("不限，例如 10M（所有任务合计）")
        if settings.global_speed_limit:
            self.global_limit_edit.setText(f"{settings.global_speed_limit // 1024}K")

        self._bandwidth_windows = [dict(item) for item in settings.bandwidth_windows]
        self.bandwidth_button = QPushButton("带宽时段…")
        self.bandwidth_button.clicked.connect(self._edit_bandwidth)
        bandwidth_row = QHBoxLayout()
        bandwidth_row.addWidget(self.bandwidth_button)
        bandwidth_row.addStretch(1)

        self.host_conns = QSpinBox()
        self.host_conns.setRange(0, 64)
        self.host_conns.setSpecialValueText("不限")
        self.host_conns.setValue(settings.max_host_connections or 0)
        self.host_conns.setToolTip("同一主机上所有任务合计的连接数上限")

        self._proxy_groups = [dict(item) for item in settings.proxy_groups]
        self.proxy_group_button = QPushButton("代理分组…")
        self.proxy_group_button.clicked.connect(self._edit_proxy_groups)
        proxy_group_row = QHBoxLayout()
        proxy_group_row.addWidget(self.proxy_group_button)
        proxy_group_row.addStretch(1)

        self._host_rules = [dict(item) for item in settings.host_rules]
        self.policy_button = QPushButton("编辑主机规则…")
        self.policy_button.clicked.connect(self._edit_host_rules)
        policy_row = QHBoxLayout()
        policy_row.addWidget(self.policy_button)
        policy_row.addStretch(1)

        self.auto_retry_check = QCheckBox("失败任务自动重试（仅瞬时错误）")
        self.auto_retry_check.setChecked(settings.auto_retry)
        self.auto_retry_max = QSpinBox()
        self.auto_retry_max.setRange(0, 10)
        self.auto_retry_max.setValue(settings.auto_retry_max)
        self.auto_retry_max.setSuffix(tr(" 次"))
        self.auto_retry_delay = QSpinBox()
        self.auto_retry_delay.setRange(5, 3600)
        self.auto_retry_delay.setValue(settings.auto_retry_delay)
        self.auto_retry_delay.setSuffix(tr(" 秒"))
        self.auto_retry_delay.setToolTip("首次重试等待时间，之后每次翻倍（上限 1 小时）")

        self.shutdown_delay = QSpinBox()
        self.shutdown_delay.setRange(5, 3600)
        self.shutdown_delay.setValue(max(5, settings.shutdown_delay))
        self.shutdown_delay.setSuffix(tr(" 秒"))
        self.shutdown_delay.setToolTip(
            "所有任务完成，且没有等待、暂停、计划或重试任务时，倒计时结束即关机。"
            "在“下载”菜单中开启或取消；该开关只对本次运行有效。"
        )
        self.mse_archive_check = QCheckBox("MSE 捕获自动保存到下载目录")
        self.mse_archive_check.setChecked(settings.mse_archive)
        self.mse_archive_check.setToolTip(
            "捕获完成后在后台把合并文件（没有合并时为主轨）移动到下载目录/分类；"
            "源轨保留在捕获目录。"
        )
        self.mse_delete_sources_check = QCheckBox("归档成功后删除已合并的源轨")
        self.mse_delete_sources_check.setChecked(settings.mse_delete_sources)
        self.mse_delete_sources_check.setToolTip(
            "仅在合并文件成功归档后删除 capture/mse 中的原始视频/音频轨；"
            "未合并、归档失败或关闭归档时保留。"
        )

        self.clipboard_check = QCheckBox("监视剪贴板中的下载链接")
        self.clipboard_check.setChecked(settings.clipboard_monitor)
        self.tray_check = QCheckBox("最小化到系统托盘")
        self.tray_check.setChecked(settings.minimize_to_tray)
        self.close_tray_check = QCheckBox("关闭窗口时最小化到托盘")
        self.close_tray_check.setChecked(settings.close_to_tray)
        self.verify_check = QCheckBox("校验 TLS 证书")
        self.verify_check.setChecked(settings.verify_tls)

        self.theme_combo = QComboBox()
        for label, value in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
            self.theme_combo.addItem(tr(label), value)
        index = self.theme_combo.findData(settings.theme)
        self.theme_combo.setCurrentIndex(max(0, index))

        self.language_combo = QComboBox()
        for label, value in (
            ("跟随系统", "system"),
            ("简体中文", "zh"),
            ("English", "en"),
        ):
            self.language_combo.addItem(tr(label), value)
        index = self.language_combo.findData(settings.language)
        self.language_combo.setCurrentIndex(max(0, index))

        form = QFormLayout()
        form.addRow("默认下载目录", dir_row)
        form.addRow("默认分类", category_row)
        form.addRow("", self.categories_check)
        form.addRow("默认连接数", self.connections)
        form.addRow("同时下载任务数", concurrent_row)
        form.addRow("默认限速", self.limit_edit)
        form.addRow("User-Agent", self.ua_edit)
        form.addRow("默认代理", self.proxy_edit)
        form.addRow("全局限速", self.global_limit_edit)
        form.addRow("更新清单", self.update_manifest_edit)
        form.addRow("ffmpeg 路径", self.ffmpeg_edit)
        form.addRow("带宽时段", bandwidth_row)
        form.addRow("每主机连接上限", self.host_conns)
        form.addRow("代理分组", proxy_group_row)
        form.addRow("主机规则", policy_row)
        form.addRow("", self.auto_retry_check)
        form.addRow("最多自动重试", self.auto_retry_max)
        form.addRow("首次重试间隔", self.auto_retry_delay)
        form.addRow("关机倒计时", self.shutdown_delay)
        form.addRow("", self.mse_archive_check)
        form.addRow("", self.mse_delete_sources_check)
        form.addRow("", self.clipboard_check)
        form.addRow("", self.tray_check)
        form.addRow("", self.close_tray_check)
        form.addRow("", self.verify_check)
        form.addRow("主题", self.theme_combo)
        form.addRow("语言", self.language_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _browse_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择默认下载目录", self.dir_edit.text()
        )
        if directory:
            self.dir_edit.setText(directory)

    def _edit_host_rules(self) -> None:
        dialog = HostPolicyDialog(self._host_rules, self._proxy_groups, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._host_rules = dialog.rules
            self.policy_button.setText(
                tr("编辑主机规则…（{} 条）").format(len(self._host_rules))
            )

    def _edit_proxy_groups(self) -> None:
        dialog = ProxyGroupDialog(self._proxy_groups, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._proxy_groups = dialog.groups
            self.proxy_group_button.setText(
                tr("代理分组…（{} 个）").format(len(self._proxy_groups))
            )

    def _reload_category_combo(self, selected: str) -> None:
        combo = self.default_category_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(tr("自动（按扩展名）"), AUTO)
        combo.addItem(tr("不分类"), NONE)
        for item in self._categories:
            combo.addItem(f"{item['name']}（{item['folder']}）", item["name"])
        index = combo.findData(selected)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _edit_categories(self) -> None:
        dialog = CategoryDialog(self._categories, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        current = str(self.default_category_combo.currentData() or AUTO)
        self._categories = dialog.categories
        self._reload_category_combo(current)
        self.category_button.setText(
            tr("分类目录…（{} 个）").format(len(self._categories))
        )

    def _edit_queues(self) -> None:
        dialog = QueueDialog(self._queues, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._queues = dialog.queues
            self.queue_button.setText(
                tr("管理队列…（{} 个）").format(len(self._queues))
            )

    def _edit_bandwidth(self) -> None:
        dialog = BandwidthDialog(self._bandwidth_windows, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._bandwidth_windows = dialog.windows
            self.bandwidth_button.setText(
                tr("带宽时段…（{} 条）").format(len(self._bandwidth_windows))
            )

    def accept(self) -> None:  # noqa: D401
        directory = Path(self.dir_edit.text().strip() or self.settings.download_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self, tr("输入有误"), tr("无法创建目录：{}").format(exc)
            )
            return
        try:
            limit = parse_limit(self.limit_edit.text())
            global_limit = parse_limit(self.global_limit_edit.text())
        except (TypeError, ValueError):
            QMessageBox.warning(
                self, tr("输入有误"), tr("限速格式不正确，例如 5M。")
            )
            return

        self.settings.download_dir = str(directory)
        self.settings.categories_enabled = self.categories_check.isChecked()
        self.settings.categories = list(self._categories)
        self.settings.default_category = str(
            self.default_category_combo.currentData() or AUTO
        )
        self.settings.global_speed_limit = global_limit
        self.settings.max_host_connections = self.host_conns.value() or None
        known_groups = {str(item.get("name")) for item in self._proxy_groups}
        dangling = sorted(
            {
                str(item.get("proxy_group"))
                for item in self._host_rules
                if isinstance(item, dict)
                and item.get("proxy_group")
                and str(item.get("proxy_group")) not in known_groups
            }
        )
        if dangling:
            QMessageBox.warning(
                self,
                tr("输入有误"),
                tr("主机规则引用了不存在的代理组：") + "、".join(dangling),
            )
            return
        self.settings.proxy_groups = list(self._proxy_groups)
        self.settings.host_rules = list(self._host_rules)
        self.settings.queues = list(self._queues)
        self.settings.bandwidth_windows = list(self._bandwidth_windows)
        self.settings.default_connections = self.connections.value()
        self.settings.max_concurrent = self.concurrent.value()
        self.settings.speed_limit = limit
        self.settings.auto_retry = self.auto_retry_check.isChecked()
        self.settings.auto_retry_max = self.auto_retry_max.value()
        self.settings.auto_retry_delay = self.auto_retry_delay.value()
        self.settings.shutdown_delay = self.shutdown_delay.value()
        self.settings.mse_archive = self.mse_archive_check.isChecked()
        self.settings.mse_delete_sources = self.mse_delete_sources_check.isChecked()
        self.settings.user_agent = self.ua_edit.text().strip() or DEFAULT_USER_AGENT
        self.settings.proxy = self.proxy_edit.text().strip() or None
        self.settings.clipboard_monitor = self.clipboard_check.isChecked()
        self.settings.minimize_to_tray = self.tray_check.isChecked()
        self.settings.close_to_tray = self.close_tray_check.isChecked()
        self.settings.verify_tls = self.verify_check.isChecked()
        self.settings.update_manifest = self.update_manifest_edit.text().strip()
        self.settings.ffmpeg_path = self.ffmpeg_edit.text().strip()
        self.settings.theme = str(self.theme_combo.currentData())
        previous_language = self.settings.language
        self.settings.language = str(self.language_combo.currentData() or "system")
        if self.settings.language != previous_language:
            QMessageBox.information(
                self,
                tr("语言"),
                tr("语言更改将在重启后生效。"),
            )
        super().accept()


class ScheduleDialog(QDialog):
    """Pick a planned start time, or drop an existing plan."""

    SET = "set"
    CLEAR = "clear"

    def __init__(
        self,
        current: float | None = None,
        repeat: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("定时开始")
        self.setMinimumWidth(520)
        self.choice: str | None = None
        self.when: float | None = None
        self.repeat: dict | None = None

        if current:
            moment = QDateTime.fromSecsSinceEpoch(int(current))
        else:
            moment = QDateTime.currentDateTime().addSecs(3600)
        self.edit = QDateTimeEdit(moment)
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.repeat_editor = RepeatEditor(self.edit)
        self.repeat_editor.set_value(repeat)

        row = QHBoxLayout()
        row.addWidget(self.edit, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        clear = buttons.addButton(
            "取消计划", QDialogButtonBox.ButtonRole.ActionRole
        )
        clear.clicked.connect(self._clear)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.repeat_editor)
        layout.addWidget(
            QLabel(
                "选择重复后，任务每次完成（或重试耗尽）都会自动排到下一个"
                "匹配的时间；“取消计划”会同时关闭重复。"
            )
        )
        layout.addWidget(buttons)

    def _clear(self) -> None:
        self.choice = self.CLEAR
        self.when = None
        self.repeat = None
        super().accept()

    def accept(self) -> None:  # noqa: D401
        try:
            repeat = self.repeat_editor.value()
        except ValueError as exc:
            QMessageBox.warning(self, tr("输入有误"), str(exc))
            return
        if repeat is not None:
            rule = parse_repeat(repeat)
            assert rule is not None
            self.choice = self.SET
            self.repeat = repeat
            self.when = rule.next_after()
            super().accept()
            return
        when = float(self.edit.dateTime().toSecsSinceEpoch())
        if when <= time.time():
            QMessageBox.warning(
                self, tr("输入有误"), tr("计划开始时间必须晚于当前时间。")
            )
            return
        self.choice = self.SET
        self.when = when
        self.repeat = None
        super().accept()


class ProxyGroupDialog(QDialog):
    """Editor for named proxies that host rules can share."""

    COLUMNS = ["名称", "代理地址"]

    def __init__(
        self, groups: list[dict], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("代理分组")
        self.resize(560, 300)
        self.groups: list[dict[str, str]] = [
            dict(item) for item in groups if isinstance(item, dict)
        ]

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 140)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        for item in self.groups:
            self._add_row(item)

        add_button = QPushButton("添加")
        remove_button = QPushButton("删除选中")
        add_button.clicked.connect(
            lambda: self._add_row({"name": "", "proxy": "http://127.0.0.1:8080"})
        )
        remove_button.clicked.connect(self._remove_selected)
        row = QHBoxLayout()
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "代理分组只保存名称和代理地址；主机规则里填写“代理组”即可"
                "复用。修改一个分组会同时影响引用它的所有主机规则。"
            )
        )
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, item: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(item.get("name") or "")))
        self.table.setItem(row, 1, QTableWidgetItem(str(item.get("proxy") or "")))
        self.table.setCurrentCell(row, 0)

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def accept(self) -> None:  # noqa: D401
        groups: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in range(self.table.rowCount()):
            name = self._cell(row, 0)
            proxy = self._cell(row, 1)
            if not name and not proxy:
                continue
            if not name:
                QMessageBox.warning(
                    self,
                    tr("代理分组"),
                    tr("第 {} 行缺少名称。").format(row + 1),
                )
                return
            if not proxy:
                QMessageBox.warning(
                    self,
                    tr("代理分组"),
                    tr("第 {} 行缺少代理地址。").format(row + 1),
                )
                return
            if name in seen:
                QMessageBox.warning(
                    self, tr("代理分组"), tr("名称重复：{}").format(name)
                )
                return
            seen.add(name)
            groups.append({"name": name[:40], "proxy": proxy})
        self.groups = groups
        super().accept()


class HostPolicyDialog(QDialog):
    """Table editor for per-host connection/limit/proxy rules.

    Rules are matched top-to-bottom against the request host with fnmatch
    (``example.com``, ``*.cdn.example.com``); the first match wins.  Empty
    fields inherit the global settings.  A rule may point at a named proxy
    group instead of spelling out the proxy URL.
    """

    COLUMNS = [
        "主机模式",
        "连接数",
        "限速",
        "代理",
        "代理组",
        "User-Agent",
        "Cookie",
        "认证（用户:密码）",
        "认证类型",
    ]

    def __init__(
        self,
        rules: list[dict],
        proxy_groups: list[dict] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("站点策略")
        self.resize(1180, 380)
        self.rules: list[dict] = [dict(item) for item in rules if isinstance(item, dict)]
        self.proxy_groups = normalize_proxy_groups(proxy_groups)

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 2, 4, 7, 8):
            self.table.setColumnWidth(column, 130 if column != 8 else 90)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        for rule in self.rules:
            self._add_row(rule)

        add_button = QPushButton("添加")
        remove_button = QPushButton("删除选中")
        add_button.clicked.connect(lambda: self._add_row({}))
        remove_button.clicked.connect(self._remove_selected)
        edit_row = QHBoxLayout()
        edit_row.addWidget(add_button)
        edit_row.addWidget(remove_button)
        edit_row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "模式支持 * 通配（example.com、*.cdn.example.com），首个匹配生效。"
                "连接数/限速/代理留空表示沿用全局；UA/Cookie/认证留空表示沿用任务"
                "自身设置。认证格式为“用户:密码”。代理组填写“代理分组…”里的"
                "名称；同时填了代理和代理组时，直接代理优先。"
            )
        )
        layout.addWidget(self.table, 1)
        layout.addLayout(edit_row)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _add_row(self, rule: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        auth = str(rule.get("auth", "") or "")
        values = [
            str(rule.get("pattern", "") or ""),
            "" if rule.get("connections") in (None, "") else str(rule.get("connections")),
            "" if rule.get("speed_limit") in (None, "") else str(rule.get("speed_limit")),
            str(rule.get("proxy", "") or ""),
            str(rule.get("proxy_group", "") or ""),
            str(rule.get("user_agent", "") or ""),
            str(rule.get("cookie", "") or ""),
            auth,
            str(rule.get("auth_type", "") or ("basic" if auth else "")),
        ]
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.setCurrentCell(row, 0)

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def accept(self) -> None:  # noqa: D401
        rules: list[dict] = []
        for row in range(self.table.rowCount()):
            pattern = self._cell(row, 0)
            connections_text = self._cell(row, 1)
            limit_text = self._cell(row, 2)
            proxy = self._cell(row, 3) or None
            proxy_group = self._cell(row, 4) or None
            user_agent = self._cell(row, 5) or None
            cookie = self._cell(row, 6) or None
            auth = self._cell(row, 7) or None
            auth_type = self._cell(row, 8) or "basic"
            if not pattern:
                if not (
                    connections_text or limit_text or proxy or proxy_group
                    or user_agent or cookie or auth
                ):
                    continue  # blank row, ignore
                self._warn(tr("第 {} 行缺少主机模式。").format(row + 1))
                return
            if proxy_group and proxy_group not in self.proxy_groups:
                self._warn(
                    tr(
                        "第 {} 行的代理组“{}”不存在，请先在“代理分组…”里添加。"
                    ).format(row + 1, proxy_group)
                )
                return
            if proxy and proxy_group:
                self._warn(
                    tr(
                        "第 {} 行同时填写了代理和代理组，将使用直接填写的代理。"
                    ).format(row + 1)
                )
            connections = None
            if connections_text:
                try:
                    connections = int(connections_text)
                except ValueError:
                    self._warn(
                        tr("第 {} 行连接数必须是整数。").format(row + 1)
                    )
                    return
            if auth and ":" not in auth:
                self._warn(
                    tr("第 {} 行认证格式应为“用户:密码”。").format(row + 1)
                )
                return
            try:
                limit = parse_limit(limit_text)
                rule = HostRule(
                    pattern=pattern,
                    connections=connections,
                    speed_limit=limit,
                    proxy=proxy,
                    proxy_group=proxy_group,
                    user_agent=user_agent,
                    cookie=cookie,
                    auth=auth,
                    auth_type=auth_type,
                )
            except (TypeError, ValueError) as exc:
                self._warn(tr("第 {} 行无效：{}").format(row + 1, exc))
                return
            rules.append(rule.to_dict())
        self.rules = rules
        super().accept()

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, tr("主机策略"), text)


class QueueDialog(QDialog):
    """Editor for extra download queues (the main queue is in Settings)."""

    COLUMNS = ["队列名", "同时任务数"]

    def __init__(self, queues: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("下载队列")
        self.resize(420, 280)
        self.queues: list[dict] = [dict(item) for item in queues if isinstance(item, dict)]

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        for item in self.queues:
            self._add_row(item)

        add_button = QPushButton("添加")
        remove_button = QPushButton("删除选中")
        add_button.clicked.connect(lambda: self._add_row({"name": "", "concurrent": 2}))
        remove_button.clicked.connect(self._remove_selected)
        row = QHBoxLayout()
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "“主队列”的并发数在设置里用“同时下载任务数”调整；"
                "这里添加额外队列。任务可在右键菜单里移动到其他队列。"
            )
        )
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, item: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        name = str(item.get("name") or "")
        concurrent = item.get("concurrent") or 2
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(str(concurrent)))

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def accept(self) -> None:  # noqa: D401
        queues: list[dict] = []
        seen: set[str] = set()
        for row in range(self.table.rowCount()):
            name = self._cell(row, 0)
            text = self._cell(row, 1)
            if not name and not text:
                continue
            if not name:
                QMessageBox.warning(
                    self,
                    tr("下载队列"),
                    tr("第 {} 行缺少队列名。").format(row + 1),
                )
                return
            if name == "主队列":
                QMessageBox.warning(
                    self,
                    tr("下载队列"),
                    tr("“主队列”由设置里的并发数控制，不能在这里添加。"),
                )
                return
            if name in seen:
                QMessageBox.warning(
                    self, tr("下载队列"), tr("队列名重复：{}").format(name)
                )
                return
            try:
                concurrent = int(text or 0)
            except ValueError:
                QMessageBox.warning(
                    self,
                    tr("下载队列"),
                    tr("第 {} 行的并发数必须是整数。").format(row + 1),
                )
                return
            if not 1 <= concurrent <= 10:
                QMessageBox.warning(
                    self, tr("下载队列"), tr("并发数必须在 1–10 之间。")
                )
                return
            seen.add(name)
            queues.append({"name": name[:40], "concurrent": concurrent})
        self.queues = queues
        super().accept()


class BandwidthDialog(QDialog):
    """Editor for time-of-day speed limits."""

    COLUMNS = ["开始 (HH:MM)", "结束 (HH:MM)", "限速（留空=不限）"]

    def __init__(self, windows: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("带宽时段")
        self.resize(480, 300)
        self.windows: list[dict] = [
            dict(item) for item in windows if isinstance(item, dict)
        ]

        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        for column in range(len(self.COLUMNS)):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.table.verticalHeader().setVisible(False)
        for item in self.windows:
            self._add_row(item)

        add_button = QPushButton("添加")
        remove_button = QPushButton("删除选中")
        add_button.clicked.connect(
            lambda: self._add_row({"start": "09:00", "end": "18:00", "limit": "1M"})
        )
        remove_button.clicked.connect(self._remove_selected)
        row = QHBoxLayout()
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "时段每天生效；开始晚于结束表示跨午夜（例如 22:00–06:00）。"
                "重叠时取最小限速；时段外使用“全局限速”。修改在 30 秒内生效。"
            )
        )
        layout.addWidget(self.table, 1)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, item: dict) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        limit = item.get("limit")
        self.table.setItem(row, 0, QTableWidgetItem(str(item.get("start") or "")))
        self.table.setItem(row, 1, QTableWidgetItem(str(item.get("end") or "")))
        self.table.setItem(
            row, 2, QTableWidgetItem("" if limit in (None, 0) else str(limit))
        )

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectionModel().selectedRows()},
            reverse=True,
        )
        for row in rows:
            self.table.removeRow(row)

    def _cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item is not None else ""

    def accept(self) -> None:  # noqa: D401
        windows: list[dict] = []
        for row in range(self.table.rowCount()):
            start_text = self._cell(row, 0)
            end_text = self._cell(row, 1)
            limit_text = self._cell(row, 2)
            if not (start_text or end_text or limit_text):
                continue
            start = parse_hhmm(start_text)
            end = parse_hhmm(end_text)
            if start is None or end is None:
                QMessageBox.warning(
                    self,
                    tr("带宽时段"),
                    tr("第 {} 行时间格式应为 HH:MM。").format(row + 1),
                )
                return
            if start == end:
                QMessageBox.warning(
                    self,
                    tr("带宽时段"),
                    tr("第 {} 行开始和结束不能相同。").format(row + 1),
                )
                return
            try:
                limit = parse_limit(limit_text)
            except (TypeError, ValueError):
                QMessageBox.warning(
                    self,
                    tr("带宽时段"),
                    tr("第 {} 行限速格式不正确，例如 1M。").format(row + 1),
                )
                return
            windows.append(
                {"start": start_text, "end": end_text, "limit": limit}
            )
        self.windows = windows
        super().accept()
