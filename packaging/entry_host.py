"""PyInstaller entry point for the browser native messaging host."""

import sys

from personal_download_manager.browser_host import main

if __name__ == "__main__":
    sys.exit(main())
