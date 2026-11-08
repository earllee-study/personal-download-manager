"""PyInstaller entry point for the desktop GUI."""

import sys

from personal_download_manager.gui import main

if __name__ == "__main__":
    sys.exit(main())
