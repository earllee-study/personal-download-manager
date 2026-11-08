"""macOS-inspired theme, icon and layout polish checks."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QTableWidget,
)

from personal_download_manager.gui.theme import (
    DARK_QSS,
    LIGHT_QSS,
    apply_theme,
    polish_widget_tree,
    set_label_tone,
)
from personal_download_manager.gui.widgets import symbol_icon


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    apply_theme(app, "light")


def test_apply_theme_sets_macos_palettes_and_styles(qapp):
    assert apply_theme(qapp, "light") == "light"
    assert qapp.palette().color(QPalette.ColorRole.Window) == QColor("#F5F5F7")
    assert "#007AFF" in LIGHT_QSS
    assert apply_theme(qapp, "dark") == "dark"
    assert qapp.palette().color(QPalette.ColorRole.Window) == QColor("#1C1C1E")
    assert "#0A84FF" in DARK_QSS
    apply_theme(qapp, "light")


def test_polish_widget_tree_right_aligns_forms_and_flattens_tables(qapp):
    dialog = QDialog()
    form = QFormLayout(dialog)
    form.addRow("Label", QLineEdit())
    table = QTableWidget(2, 2, dialog)
    polish_widget_tree(dialog)
    try:
        assert form.labelAlignment() & Qt.AlignmentFlag.AlignRight
        assert table.frameShape() == QFrame.Shape.NoFrame
        assert not table.showGrid()
        assert not table.alternatingRowColors()
        assert table.verticalHeader().isHidden()
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_label_tones_and_symbol_icons(qapp):
    label = QLabel("x")
    set_label_tone(label, "error")
    assert label.property("tone") == "error"
    set_label_tone(label, "secondary")
    assert label.property("tone") == "secondary"
    label.deleteLater()

    names = (
        "add",
        "play",
        "pause",
        "trash",
        "file",
        "folder",
        "clock",
        "sliders",
        "check",
        "copy",
        "info",
        "refresh",
        "batch",
    )
    for name in names:
        icon = symbol_icon(name, QColor("#1D1D1F"))
        pixmap = icon.pixmap(16, 16)
        assert not pixmap.isNull()
        image = pixmap.toImage()
        colored = any(
            image.pixelColor(x, y).alpha() > 0
            for x in range(image.width())
            for y in range(image.height())
        )
        assert colored, name


def test_main_window_uses_card_and_polished_table(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window
    from personal_download_manager.gui.settings import AppSettings

    monkeypatch.setattr(main_window, "capture_dir", lambda: tmp_path / "capture")
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)
    settings = AppSettings(download_dir=str(tmp_path), language="zh")
    settings.save = lambda path=None: None
    window = main_window.MainWindow(settings)
    try:
        assert window.table_card.objectName() == "ContentCard"
        assert window.table.frameShape() == QFrame.Shape.NoFrame
        assert window.table.verticalHeader().isHidden()
        assert window.table.verticalHeader().defaultSectionSize() == 36
    finally:
        window.manager.shutdown()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window.hide()
        window.deleteLater()
        QApplication.processEvents()
