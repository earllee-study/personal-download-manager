"""Filesystem locations shared by the GUI, the native host and tests."""

from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    """%APPDATA%\\PersonalDownloadManager on Windows, ~/.personal-download-manager elsewhere."""
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "PersonalDownloadManager"
    return Path.home() / ".personal-download-manager"


def capture_dir() -> Path:
    """Drop directory where the browser native host leaves new downloads."""
    return app_data_dir() / "capture"
