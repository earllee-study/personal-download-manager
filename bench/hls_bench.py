"""Reproducible HLS segment-concurrency benchmark.

Eight 512 KiB segments, each connection throttled to 1 MiB/s.  Sequential
(one connection) needs ~4 s; four segment workers should finish in ~1 s.

Run from the project root:
    python bench/hls_bench.py
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

from personal_download_manager.hls import download_hls  # noqa: E402
from personal_download_manager.models import DownloadConfig  # noqa: E402

logging.getLogger("PDM").setLevel(logging.ERROR)

SEGMENTS = 8
SEGMENT_SIZE = 512 * 1024
THROTTLE = 512 * 1024  # 0.5 MiB/s per connection -> 1s per segment
RUNS = 3


def run(server: TestServer, directory: Path, connections: int) -> list[float]:
    times: list[float] = []
    for index in range(RUNS):
        output = directory / f"hls-{connections}-{index}.ts"
        output.unlink(missing_ok=True)
        config = DownloadConfig(
            url=server.url("/hls/media.m3u8"),
            output=output,
            max_connections=connections,
            show_progress=False,
            quiet=True,
            timeout=30.0,
        )
        started = time.monotonic()
        download_hls(config)
        times.append(time.monotonic() - started)
        if output.stat().st_size != SEGMENTS * SEGMENT_SIZE:
            raise RuntimeError("HLS benchmark produced a truncated file")
    return times


def main() -> int:
    server = TestServer().start()
    try:
        for index in range(SEGMENTS):
            server.add(
                f"/hls/seg{index}.ts",
                Route(
                    content=make_content(SEGMENT_SIZE, seed=600 + index),
                    throttle_bps=THROTTLE,
                ),
            )
        playlist = server.add("/hls/media.m3u8", Route(content=b""))
        playlist.content = (
            "#EXTM3U\n"
            + "".join(f"#EXTINF:1,\nseg{index}.ts\n" for index in range(SEGMENTS))
            + "#EXT-X-ENDLIST\n"
        ).encode()

        print(
            f"{SEGMENTS} segments x {SEGMENT_SIZE / 1024:.0f} KiB, "
            f"{THROTTLE / 1024:.0f} KiB/s per connection, {RUNS} runs (median)\n"
        )
        with tempfile.TemporaryDirectory(prefix="pdm-hls-") as tmp:
            directory = Path(tmp)
            sequential = run(server, directory, 1)
            parallel = run(server, directory, 4)
        print(f"{'connections':>12} {'elapsed':>9}")
        for label, times in (("1", sequential), ("4", parallel)):
            print(f"{label:>12} {statistics.median(times):>8.2f}s")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
