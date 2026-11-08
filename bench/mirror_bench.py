"""Reproducible multi-source benchmark.

Two local servers host the identical file.  A per-host connection cap (4) and
a per-connection throttle (2 MiB/s) are applied, which is the situation
mirrors exist for: one host alone cannot exceed cap x per-connection speed,
but two hosts can.

The second server is reached as ``localhost`` (not 127.0.0.1) so the host
policy treats it as a separate host, exactly like two real mirrors.

Run from the project root:
    python bench/mirror_bench.py
"""

from __future__ import annotations

import logging
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from server import Route, TestServer, make_content  # noqa: E402

from personal_download_manager import DownloadConfig, Downloader  # noqa: E402
from personal_download_manager.policy import Governor, HostPolicies  # noqa: E402

logging.getLogger("PDM").setLevel(logging.ERROR)

TOTAL = 32 * 1024 * 1024
PER_CONN = 2 * 1024 * 1024
HOST_CAP = 4
RUNS = 3


def run(url: str, mirrors: list[str], directory: Path, tag: str) -> list[float]:
    times: list[float] = []
    for index in range(RUNS):
        output = directory / f"{tag}-{index}.bin"
        for suffix in ("", ".pdm.part", ".pdm.json"):
            Path(str(output) + suffix).unlink(missing_ok=True)
        governor = Governor(HostPolicies(default_connections=HOST_CAP))
        config = DownloadConfig(
            url=url,
            output=output,
            max_connections=8,
            min_segment_size=1024 * 1024,
            min_split_size=1024 * 1024,
            show_progress=False,
            quiet=True,
            timeout=30.0,
            mirrors=mirrors,
            governor=governor,
        )
        started = time.monotonic()
        Downloader(config).run()
        times.append(time.monotonic() - started)
        if output.stat().st_size != TOTAL:
            raise RuntimeError(f"{tag}: truncated output")
    return times


def main() -> int:
    primary = TestServer().start()
    secondary = TestServer().start()
    try:
        content = make_content(TOTAL, seed=505)
        primary.add("/mirror.bin", Route(content=content, etag='"mirror"', throttle_bps=PER_CONN))
        secondary.add(
            "/mirror.bin", Route(content=content, etag='"mirror"', throttle_bps=PER_CONN)
        )
        primary_url = primary.url("/mirror.bin")
        mirror_url = f"http://localhost:{secondary.port}/mirror.bin"

        print(
            f"{TOTAL / 1048576:.0f} MiB, {PER_CONN / 1048576:.0f} MiB/s per connection, "
            f"per-host connection cap {HOST_CAP}, {RUNS} runs (median)\n"
        )
        with tempfile.TemporaryDirectory(prefix="pdm-mirror-") as tmp:
            directory = Path(tmp)
            single = run(primary_url, [], directory, "single")
            dual = run(primary_url, [mirror_url], directory, "dual")

        rows = [("single source", single), ("two mirrors", dual)]
        print(f"{'mode':>14} {'elapsed':>9} {'speed':>12}")
        for label, times in rows:
            median = statistics.median(times)
            print(
                f"{label:>14} {median:>8.2f}s "
                f"{TOTAL / median / 1048576:>9.2f} MiB/s"
            )
    finally:
        primary.stop()
        secondary.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
