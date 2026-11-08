"""macOS-inspired light/dark palettes and Qt stylesheet.

The design keeps the native window frame (resize, snapping, accessibility) and
styles the content: SF/Segoe UI-style typography, soft grouped backgrounds,
rounded controls and cards, hairline separators, the macOS accent blue and the
system status colors.  ``polish_widget_tree`` applies layout-only details that
QSS cannot express (right-aligned form labels, borderless tables).
"""

from __future__ import annotations

from string import Template

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QFrame,
    QTableView,
    QWidget,
)

LIGHT = {
    "window": "#F5F5F7",
    "card": "#FFFFFF",
    "card_alt": "#FAFAFC",
    "text": "#1D1D1F",
    "secondary": "#6E6E73",
    "disabled": "#AEAEB2",
    "separator": "#E5E5EA",
    "control": "#FFFFFF",
    "control_border": "#D1D1D6",
    "hover": "#EFEFF4",
    "pressed": "#E2E2E8",
    "accent": "#007AFF",
    "accent_hover": "#147CE5",
    "accent_pressed": "#0062CC",
    "track": "#E5E5EA",
    "scroll": "rgba(60, 60, 67, 0.35)",
    "scroll_hover": "rgba(60, 60, 67, 0.55)",
    "danger": "#FF3B30",
    "warning": "#FF9F0A",
    "tooltip_bg": "#1D1D1F",
    "tooltip_text": "#FFFFFF",
}

DARK = {
    "window": "#1C1C1E",
    "card": "#2C2C2E",
    "card_alt": "#242426",
    "text": "#F5F5F7",
    "secondary": "#98989D",
    "disabled": "#636366",
    "separator": "#38383A",
    "control": "#2C2C2E",
    "control_border": "#3A3A3C",
    "hover": "#3A3A3C",
    "pressed": "#48484A",
    "accent": "#0A84FF",
    "accent_hover": "#3395FF",
    "accent_pressed": "#0060DF",
    "track": "#3A3A3C",
    "scroll": "rgba(235, 235, 245, 0.30)",
    "scroll_hover": "rgba(235, 235, 245, 0.50)",
    "danger": "#FF453A",
    "warning": "#FF9F0A",
    "tooltip_bg": "#3A3A3C",
    "tooltip_text": "#F5F5F7",
}

_QSS = Template(
    """
QMainWindow, QDialog { background: $window; color: $text; }
QWidget { color: $text; }
QWidget:disabled { color: $disabled; }

QMenuBar { background: transparent; color: $text; padding: 2px 6px; spacing: 2px; border: 0; }
QMenuBar::item { padding: 5px 10px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: $hover; }
QMenuBar::item:pressed { background: $pressed; }

QMenu { background: $card; color: $text; border: 1px solid $separator;
        border-radius: 8px; padding: 5px; }
QMenu::item { padding: 6px 26px 6px 12px; border-radius: 5px; background: transparent; }
QMenu::item:selected { background: $accent; color: white; }
QMenu::item:disabled { color: $disabled; }
QMenu::separator { height: 1px; background: $separator; margin: 4px 8px; }

QToolBar { background: transparent; border: 0; padding: 5px 10px; spacing: 3px; }
QToolBar::separator { width: 1px; background: $separator; margin: 6px 4px; }
QToolButton { background: transparent; color: $text; padding: 6px 9px;
              border: 0; border-radius: 7px; }
QToolButton:hover { background: $hover; }
QToolButton:pressed { background: $pressed; }
QToolButton:checked { background: $pressed; }
QToolButton:disabled { color: $disabled; }

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: $control; color: $text; border: 1px solid $control_border;
    border-radius: 8px; padding: 5px 9px;
    selection-background-color: $accent; selection-color: white;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus { border: 1px solid $accent; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled { color: $disabled; background: $hover; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox QAbstractItemView {
    background: $card; color: $text; border: 1px solid $separator;
    border-radius: 8px; padding: 4px; outline: 0;
    selection-background-color: $accent; selection-color: white;
}

QPushButton { background: $control; color: $text; border: 1px solid $control_border;
              border-radius: 7px; padding: 6px 14px; min-height: 18px; }
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $pressed; }
QPushButton:disabled { color: $disabled; background: $window; }
QPushButton:default { background: $accent; color: white; border-color: $accent; }
QPushButton:default:hover { background: $accent_hover; }
QPushButton:default:pressed { background: $accent_pressed; }
QDialogButtonBox QPushButton { min-width: 72px; }
QCheckBox, QRadioButton { spacing: 7px; }

QTableView, QTableWidget, QTreeView, QListView {
    background: transparent; color: $text; border: 0; outline: 0;
    gridline-color: transparent; alternate-background-color: $card_alt;
    selection-background-color: $accent; selection-color: white;
}
QTableWidget { background: $card; border: 1px solid $separator; border-radius: 8px; }
QTableView::item, QTableWidget::item { padding: 3px 6px; }
QTableView::item:selected, QTableWidget::item:selected { background: $accent; color: white; }
QHeaderView { background: transparent; }
QHeaderView::section { background: transparent; color: $secondary; padding: 7px 8px;
                       border: 0; border-bottom: 1px solid $separator;
                       font-weight: 600; }
QTableCornerButton::section { background: transparent; border: 0; }

QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll; min-height: 28px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: $scroll_hover; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scroll; min-width: 28px; border-radius: 4px; }
QScrollBar::handle:horizontal:hover { background: $scroll_hover; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QStatusBar { background: transparent; color: $secondary; border-top: 1px solid $separator; }
QStatusBar::item { border: 0; }

QGroupBox { background: $card; border: 1px solid $separator; border-radius: 10px;
            margin-top: 14px; padding: 14px 12px 12px 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;
                   left: 14px; top: 2px; padding: 0 4px;
                   color: $secondary; background: $card; }

QProgressBar { background: $track; border: 0; border-radius: 4px;
               height: 7px; text-align: center; color: $secondary; }
QProgressBar::chunk { background: $accent; border-radius: 4px; }

QToolTip { background: $tooltip_bg; color: $tooltip_text; border: 0;
           border-radius: 6px; padding: 5px 8px; }

QFrame#ContentCard { background: $card; border: 1px solid $separator;
                     border-radius: 12px; }
QLabel#SecondaryLabel, QLabel[tone="secondary"] { color: $secondary; }
QLabel[tone="error"] { color: $danger; }
QLabel[tone="warning"] { color: $warning; }

QTabWidget::pane { border: 1px solid $separator; border-radius: 10px; background: $card; }
QTabBar::tab { background: transparent; padding: 6px 12px; margin-right: 4px;
               border-radius: 6px; color: $secondary; }
QTabBar::tab:selected { background: $hover; color: $text; }
"""
)

LIGHT_QSS = _QSS.substitute(LIGHT)
DARK_QSS = _QSS.substitute(DARK)


def _palette(colors: dict[str, str]) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(colors["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(colors["card"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["card_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(colors["control"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["tooltip_bg"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["tooltip_text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["secondary"]))
    palette.setColor(QPalette.ColorRole.Link, QColor(colors["accent"]))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled"]))
    palette.setColor(
        QPalette.ColorGroup.Inactive,
        QPalette.ColorRole.Highlight,
        QColor(colors["control_border"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Inactive,
        QPalette.ColorRole.HighlightedText,
        QColor(colors["text"]),
    )
    return palette


def set_label_tone(label: QWidget, tone: str) -> None:
    """Apply ``secondary``/``error``/``warning`` color through QSS."""
    label.setProperty("tone", tone)
    style = label.style()
    style.unpolish(label)
    style.polish(label)


def polish_widget_tree(root: QWidget) -> None:
    """Apply layout details that a stylesheet cannot express."""
    for form in root.findChildren(QFormLayout):
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(9)
    for view in root.findChildren(QTableView):
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setShowGrid(False)
        view.setAlternatingRowColors(False)
        view.verticalHeader().setVisible(False)
        view.horizontalHeader().setHighlightSections(False)


class _PolishFilter(QObject):
    """Polish each top-level window when it is shown (dialogs included)."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            polish_widget_tree(obj)
        return False


_polish_filter: _PolishFilter | None = None


def install_ui_polish(app: QApplication) -> None:
    global _polish_filter
    if _polish_filter is None:
        _polish_filter = _PolishFilter(app)
        app.installEventFilter(_polish_filter)


def apply_theme(app: QApplication, theme: str) -> str:
    """Apply ``system``/``light``/``dark``; return the effective scheme."""
    if theme == "system":
        try:
            hints = app.styleHints()
            scheme = hints.colorScheme()
            theme = "dark" if scheme == Qt.ColorScheme.Dark else "light"
        except (AttributeError, TypeError):
            theme = "light"
    if theme == "dark":
        app.setPalette(_palette(DARK))
        app.setStyleSheet(DARK_QSS)
        return "dark"
    app.setPalette(_palette(LIGHT))
    app.setStyleSheet(LIGHT_QSS)
    return "light"
