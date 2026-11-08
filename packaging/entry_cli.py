"""PyInstaller entry point for the command-line interface."""

import sys

from personal_download_manager.cli import main

if __name__ == "__main__":
    sys.exit(main(None))
