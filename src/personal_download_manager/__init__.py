"""PDM - a multi-connection, resumable HTTP(S) download engine.

The public surface is intentionally small so the engine can be used both from
the CLI and (later) from a PySide6 GUI:

    from personal_download_manager import DownloadConfig, Downloader

    cfg = DownloadConfig(url="https://example.com/file.bin", output="file.bin")
    result = Downloader(cfg).run()
"""

from .models import DownloadConfig, DownloadResult, ProbeResult, ProgressSnapshot

__all__ = [
    "DownloadConfig",
    "DownloadResult",
    "Downloader",
    "ProbeResult",
    "ProgressSnapshot",
]

__version__ = "0.1.0"


def __getattr__(name: str):
    # Import the engine lazily: the browser native host only needs paths and
    # version information, and should not pull httpx into its process.
    if name == "Downloader":
        from .downloader import Downloader

        return Downloader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
