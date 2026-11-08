"""PySide6 desktop front-end for personal_download_manager.

Importing this package requires the optional ``gui`` extra::

    pip install "personal-download-manager[gui]"
"""

__all__ = ["main"]


def main() -> int:
    from .app import main as _main

    return _main()
