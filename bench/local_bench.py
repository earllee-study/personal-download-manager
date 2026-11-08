"""Reproducible local benchmark for segmentation and dynamic re-segmentation.

The test server throttles *each connection* independently, which is exactly the
situation where multi-connection downloading wins: a single TCP flow is capped
by the server, but N flows are not.

Run from the project root:
    python bench/local_bench.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from server import Route, TestServer, make_content  # noqa: E402

from personal_download_manager import DownloadConfig, Downloader  # noqa: E402


def run_case(
    server: TestServer,
    url_path: str,
    total: int,
    connections: int,
    dynamic: bool,
    directory: Path,
    min_split: int = 512 * 1024,
) -> tuple[float, object]:
    output = directory / f"bench_{connections}_{int(dynamic)}.bin"
    config = DownloadConfig(
        url=server.url(url_path),
        output=output,
        max_connections=connections,
        min_segment_size=max(256 * 1024, total // connections),
        min_split_size=min_split,
        dynamic_segmentation=dynamic,
        show_progress=False,
        quiet=True,
        retry_backoff=0.1,
        timeout=30.0,
        connect_timeout=10.0,
        read_timeout=30.0,
    )
    started = time.monotonic()
    result = Downloader(config).run()
    elapsed = time.monotonic() - started
    if output.stat().st_size != total:
        raise RuntimeError("benchmark produced a truncated file")
    output.unlink()
    return elapsed, result


def scaling_test(server: TestServer, directory: Path) -> None:
    total = 32 * 1024 * 1024
    per_conn = 4 * 1024 * 1024
    server.add(
        "/scale.bin",
        Route(
            content=make_content(total, seed=101),
            etag='"scale"',
            throttle_bps=per_conn,
        ),
    )
    print("Scaling: 32 MiB file, 4 MiB/s per connection")
    print(f"{'conns':>6} {'elapsed':>9} {'speed':>12} {'peak':>5} {'parts':>6}")
    for connections in (1, 2, 4, 8, 16):
        elapsed, result = run_case(
            server, "/scale.bin", total, connections, dynamic=False, directory=directory
        )
        speed = total / elapsed / (1024 * 1024)
        print(
            f"{connections:>6} {elapsed:>8.2f}s {speed:>9.2f} MiB/s "
            f"{result.peak_connections:>5} {result.segments_created:>6}"
        )
    print()


def dynamic_test(server: TestServer, directory: Path) -> None:
    total = 16 * 1024 * 1024
    route = server.add(
        "/dynamic.bin",
        Route(
            content=make_content(total, seed=202),
            etag='"dynamic"',
            throttle_bps=2 * 1024 * 1024,
            first_range_fast=True,
        ),
    )
    print("Dynamic segmentation: 16 MiB, 4 connections, one fast + three 2 MiB/s")
    print(f"{'mode':>12} {'elapsed':>9} {'speed':>12} {'parts':>6}")
    for dynamic in (False, True):
        route.range_requests = 0  # give each run its own "first fast" request
        elapsed, result = run_case(
            server,
            "/dynamic.bin",
            total,
            connections=4,
            dynamic=dynamic,
            directory=directory,
        )
        speed = total / elapsed / (1024 * 1024)
        label = "dynamic" if dynamic else "static"
        print(
            f"{label:>12} {elapsed:>8.2f}s {speed:>9.2f} MiB/s "
            f"{result.segments_created:>6}"
        )
    print()


def main() -> int:
    server = TestServer().start()
    try:
        with tempfile.TemporaryDirectory(prefix="pdm-bench-") as tmp:
            scaling_test(server, Path(tmp))
            dynamic_test(server, Path(tmp))
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
