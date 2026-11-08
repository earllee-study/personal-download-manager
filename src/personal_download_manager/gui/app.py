"""QApplication entry point."""

from __future__ import annotations

import logging
import sys

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from .i18n import install_translation_filter, set_language
from .main_window import MainWindow
from .settings import AppSettings
from .theme import apply_theme, install_ui_polish
from .widgets import make_app_icon


def _ui_font() -> QFont:
    families = set(QFontDatabase.families())
    for name in (
        "SF Pro Text",
        "SF Pro Display",
        "Segoe UI Variable Text",
        "Segoe UI",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
    ):
        if name in families:
            return QFont(name, 10)
    return QFont()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("Personal Download Manager")
    app.setOrganizationName("PDM")
    app.setApplicationDisplayName("Personal Download Manager")
    app.setStyle("Fusion")
    app.setFont(_ui_font())
    app.setWindowIcon(make_app_icon())

    settings = AppSettings.load()
    set_language(settings.language)
    install_translation_filter(app)
    install_ui_polish(app)
    apply_theme(app, settings.theme)
    window = MainWindow(settings)
    window.show()
    return app.exec()
