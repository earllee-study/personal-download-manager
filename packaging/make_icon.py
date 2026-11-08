"""Render the application icon to packaging/icon.ico (used by PyInstaller)."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from personal_download_manager.gui.widgets import make_app_icon


def main() -> int:
    app = QApplication([])  # noqa: F841 - required for QPixmap
    target = Path(__file__).with_name("icon.ico")
    icon = make_app_icon(256)
    pixmap = icon.pixmap(256, 256)
    if not pixmap.save(str(target), "ICO"):
        print(f"could not write {target}")
        return 1
    print(f"wrote {target} ({target.stat().st_size} bytes)")

    # Browser extensions need PNG icons at fixed sizes.
    extension_dir = Path(__file__).resolve().parents[1] / "browser" / "extension"
    if extension_dir.is_dir():
        for size in (16, 32, 48, 128):
            path = extension_dir / f"icon{size}.png"
            icon.pixmap(size, size).save(str(path), "PNG")
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
