"""Reproducible AIMD benchmark against a server that rejects excess concurrency.

The local server accepts at most ``max_parallel`` simultaneous range
responses and answers the rest with 503 + Retry-After.  Fixed high
concurrency therefore spends its time in retry loops; AIMD should converge to
the server's real capacity with fewer rejected requests.

Run from the project root:
    python bench/adaptive_bench.py
"""

from __future__ import annotations

import logging
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from server import Route, TestServer, make_content  # noqa: E402

from personal_download_manager import DownloadConfig, Downloader  # noqa: E402

logging.getLogger("PDM").setLevel(logging.ERROR)

TOTAL = 16 * 1024 * 1024
PER_CONN = 4 * 1024 * 1024
MAX_PARALLEL = 3
RUNS = 3


@dataclass
class Case:
    name: str
    elapsed: list[float]
    retries: list[int]
    rejections: list[int]
    peak: list[int]
    final: list[int]

    def summary(self) -> str:
        return (
            f"{self.name:>14} {statistics.median(self.elapsed):>7.2f}s "
            f"{statistics.median(self.retries):>8.0f} "
            f"{statistics.median(self.rejections):>10.0f} "
            f"{statistics.median(self.peak):>5.0f} "
            f"{statistics.median(self.final):>6.0f}"
        )


def run_case(server: TestServer, directory: Path, name: str, adaptive: bool) -> Case:
    case = Case(name, [], [], [], [], [])
    config = DownloadConfig(
        url=server.url("/congested.bin"),
        output=directory / f"{name.replace(' ', '_')}.bin",
        max_connections=8,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        adaptive=adaptive,
        adaptive_start=2,
        adaptive_interval=0.5,
        max_retries=20,
        retry_backoff=0.05,
        show_progress=False,
        quiet=True,
        timeout=30.0,
    )
    for _ in range(RUNS):
        output = Path(config.output)
        output.unlink(missing_ok=True)
        Path(str(output) + ".pdm.part").unlink(missing_ok=True)
        Path(str(output) + ".pdm.json").unlink(missing_ok=True)
        marker = len(server.requests)
        started = time.monotonic()
        result = Downloader(config).run()
        elapsed = time.monotonic() - started
        if output.stat().st_size != TOTAL:
            raise RuntimeError(f"{name}: truncated output")
        rejections = sum(
            1 for item in server.requests[marker:] if item.get("status") == 503
        )
        case.elapsed.append(elapsed)
        case.retries.append(result.retries)
        case.rejections.append(rejections)
        case.peak.append(result.peak_connections)
        case.final.append(result.final_connections)
        output.unlink(missing_ok=True)
    return case


def main() -> int:
    server = TestServer().start()
    try:
        server.add(
            "/congested.bin",
            Route(
                content=make_content(TOTAL, seed=404),
                etag='"congested"',
                throttle_bps=PER_CONN,
                max_parallel=MAX_PARALLEL,
            ),
        )
        print(
            f"{TOTAL / 1048576:.0f} MiB, {PER_CONN / 1048576:.0f} MiB/s per connection, "
            f"server accepts at most {MAX_PARALLEL} concurrent ranges, "
            f"{RUNS} runs each (median)\n"
        )
        with tempfile.TemporaryDirectory(prefix="pdm-aimd-") as tmp:
            directory = Path(tmp)
            cases = [
                run_case(server, directory, "fixed -n 8", adaptive=False),
                run_case(server, directory, "adaptive", adaptive=True),
            ]
        print(
            f"{'mode':>14} {'elapsed':>8} {'retries':>8} {'503s':>10} "
            f"{'peak':>5} {'final':>6}"
        )
        for case in cases:
            print(case.summary())
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
