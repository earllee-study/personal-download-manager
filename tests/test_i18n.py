"""GUI i18n: catalog coverage, language switching and representative screens."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from personal_download_manager.gui import i18n
from personal_download_manager.gui.dialogs import AddDownloadDialog, SettingsDialog
from personal_download_manager.gui.model import (
    STATUS_QUEUED,
    STATUS_SCHEDULED,
    DownloadTableModel,
    DownloadTask,
)
from personal_download_manager.gui.settings import AppSettings
from personal_download_manager.gui.update_dialog import UpdateDialog
from personal_download_manager.update import UpdateInfo

ROOT = Path(__file__).resolve().parents[1]
GUI_FILES = (
    "main_window.py",
    "dialogs.py",
    "model.py",
    "update_dialog.py",
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture(autouse=True)
def reset_language():
    i18n.set_language("zh")
    yield
    i18n.set_language("zh")


def test_catalog_translates_known_and_keeps_unknown():
    i18n.set_language("en")
    assert i18n.tr("添加下载") == "Add download"
    assert i18n.tr("not a catalog key") == "not a catalog key"
    assert i18n.translate_repeat("每天 03:30") == "Daily at 03:30"
    assert i18n.translate_repeat("每周 周一、周五 22:00") == (
        "Weekly Mon, Fri at 22:00"
    )
    assert i18n.translate_repeat("每隔 3 天 06:00") == "Every 3 days at 06:00"
    i18n.set_language("zh")
    assert i18n.tr("添加下载") == "添加下载"
    assert i18n.translate_repeat("每天 03:30") == "每天 03:30"


def test_catalog_covers_all_gui_chinese_literals():
    """A new Chinese UI literal must come with an English translation."""
    missing: list[str] = []
    for filename in GUI_FILES:
        path = ROOT / "src" / "personal_download_manager" / "gui" / filename
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.search(r"[一-鿿]", node.value)
                and id(node) not in docstrings
                and node.value != "主队列"  # queue data, not UI chrome
                and node.value not in i18n._EN
            ):
                missing.append(f"{filename}:{node.lineno}: {node.value!r}")
    assert not missing, "missing EN translations:\n" + "\n".join(missing)
    assert all(value for value in i18n._EN.values())


def test_language_setting_and_persistence(tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), language="fr")
    assert settings.language == "system"
    settings.language = "en"
    path = tmp_path / "config.json"
    settings.save(path)
    assert AppSettings.load(path).language == "en"
    assert i18n.resolve_language("en") == "en"
    assert i18n.resolve_language("zh") == "zh"
    assert i18n.resolve_language("bogus") in ("zh", "en")


def test_model_labels_and_tooltips_in_english(qapp):
    i18n.set_language("en")
    task = DownloadTask(
        url="https://example.com/a.bin",
        output="a.bin",
        status=STATUS_QUEUED,
        repeat={"kind": "daily", "time": "03:30"},
    )
    assert task.status_label == "Queued"
    assert task.repeat_text == "Daily at 03:30"
    model = DownloadTableModel()
    assert model.headerData(0, Qt.Orientation.Horizontal) == "File name"
    assert model.headerData(1, Qt.Orientation.Horizontal) == "Status"

    scheduled = DownloadTask(
        url="https://example.com/b.bin",
        output="b.bin",
        status=STATUS_SCHEDULED,
        scheduled_at=1790000000.0,
        repeat={"kind": "interval", "time": "06:00", "days": 2, "anchor": "2026-01-01"},
    )
    tooltip = DownloadTableModel._status_tooltip(scheduled)
    assert tooltip is not None
    assert "Scheduled start:" in tooltip
    assert "Repeat: Every 2 days at 06:00" in tooltip


def test_show_filter_translates_dialogs(qapp):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

    i18n.set_language("en")
    i18n.install_translation_filter(qapp)
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    label = QLabel("添加下载")
    layout.addWidget(label)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    layout.addWidget(buttons)
    try:
        dialog.show()
        QApplication.processEvents()
        assert label.text() == "Add download"
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert ok is not None and ok.text() == "OK"
        assert cancel is not None and cancel.text() == "Cancel"
    finally:
        dialog.close()
        dialog.deleteLater()
        QApplication.processEvents()


def test_standard_buttons_are_localized_in_both_languages(qapp):
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

    def labels() -> tuple[str, str]:
        dialog = QDialog()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        QVBoxLayout(dialog).addWidget(buttons)
        i18n.translate_widget_tree(dialog)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert ok is not None and cancel is not None
        try:
            return ok.text(), cancel.text()
        finally:
            dialog.deleteLater()

    i18n.set_language("zh")
    assert labels() == ("确定", "取消")
    i18n.set_language("en")
    assert labels() == ("OK", "Cancel")


def test_update_dialog_status_in_english(qapp):
    i18n.set_language("en")
    dialog = UpdateDialog("https://example.com/latest.json", "0.1.0", start=False)
    try:
        assert "Current version 0.1.0" in dialog.status.text()
        dialog._on_checked(None)
        assert "up to date" in dialog.status.text()
        info = UpdateInfo(
            version="0.2.0",
            url="https://example.com/setup.exe",
            sha256="0" * 64,
            size=1024,
            notes="notes",
            published_at="2026-11-01",
        )
        dialog._on_checked(info)
        assert "New version 0.2.0" in dialog.status.text()
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_add_and_settings_dialogs_in_english(qapp, tmp_path):
    i18n.set_language("en")
    settings = AppSettings(download_dir=str(tmp_path), language="en")
    add = AddDownloadDialog(settings, None, "https://example.com/a.bin")
    try:
        i18n.translate_widget_tree(add)
        assert add.windowTitle() == "Add download"
        assert add.start_check.text() == "Start downloading now"
        assert add.connections.suffix() == " connections"
        assert add.repeat_editor.kind.itemText(0) == "Do not repeat"
        assert add.repeat_editor.kind.itemText(3) == "Every N days"
        assert add.repeat_editor.days.suffix() == " days"
    finally:
        add.deleteLater()
        QApplication.processEvents()

    dialog = SettingsDialog(settings)
    try:
        i18n.translate_widget_tree(dialog)
        assert dialog.windowTitle() == "Settings"
        assert dialog.language_combo.itemText(0) == "Follow system"
        assert dialog.language_combo.itemText(1) == "Chinese (Simplified)"
        assert dialog.language_combo.itemText(2) == "English"
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_main_window_chrome_in_english(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window

    i18n.set_language("en")
    monkeypatch.setattr(main_window, "capture_dir", lambda: tmp_path / "capture")
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)
    settings = AppSettings(download_dir=str(tmp_path), language="en")
    settings.save = lambda path=None: None  # never touch the real %APPDATA%
    window = main_window.MainWindow(settings)
    try:
        assert window.windowTitle() == "Personal Download Manager — Downloader"
        assert [action.text() for action in window.menuBar().actions()] == [
            "File",
            "Downloads",
            "Help",
        ]
        assert window.add_action.text() == "Add download"
        assert window.search_edit.placeholderText() == "Search filename or URL…"
        assert window.status_left.text() in ("Ready", "0 tasks in total") or (
            "Downloading" in window.status_left.text()
        )
        assert window.summary_label.text().endswith("tasks in total")
    finally:
        window.manager.shutdown()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window.hide()
        window.deleteLater()
        QApplication.processEvents()
