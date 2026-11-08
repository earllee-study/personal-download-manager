from __future__ import annotations

from pathlib import Path

from personal_download_manager.downloader import Downloader
from personal_download_manager.models import DownloadConfig


def run_download(url: str, output: Path, **overrides) -> object:
    defaults = dict(
        url=url,
        output=output,
        show_progress=False,
        quiet=True,
        retry_backoff=0.01,
        timeout=10.0,
        connect_timeout=5.0,
        read_timeout=10.0,
    )
    defaults.update(overrides)
    return Downloader(DownloadConfig(**defaults)).run()
