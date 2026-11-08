"""HTTP/1.1 (one connection per worker) vs HTTP/2 (multiplexed) benchmark.

Both servers use TLS with the test certificate, add 50 ms of request latency
(RTT-like) and throttle every stream to 2 MiB/s.  Under those conditions the
transfer itself is identical; the difference is connection setup and whether
the segments can share one TCP/TLS connection.

Run from the project root:

    python bench/http2_bench.py            # 3 runs, median
    python bench/http2_bench.py --runs 5
    python bench/http2_bench.py --latency 0.1 --throttle 4M --size 32M
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from h2server import H2Server  # noqa: E402
from server import Route, TestServer, make_content  # noqa: E402

from personal_download_manager import DownloadConfig, Downloader  # noqa: E402

MIB = 1024 * 1024


def parse_size(text: str) -> int:
    text = text.strip().lower().replace("ib", "").replace("b", "")
    multiplier = 1
    if text and text[-1] in "kmg":
        multiplier = {"k": 1024, "m": MIB, "g": 1024 * MIB}[text[-1]]
        text = text[:-1]
    return int(float(text) * multiplier)


def run_once(
    *,
    transport: str,
    connections: int,
    data: bytes,
    latency: float,
    throttle: int | None,
    directory: Path,
) -> dict:
    name = f"{transport}-{connections}-{time.time_ns()}.bin"
    path = "/" + name
    digest = hashlib.sha256(data).hexdigest()

    if transport == "h2":
        server = H2Server(latency=latency, throttle_bps=throttle).start()
        server.add(path, data)
    else:
        server = TestServer(tls=True, latency=latency).start()
        server.add(path, Route(content=data, throttle_bps=throttle))

    output = directory / name
    config = DownloadConfig(
        url=server.url(path),
        output=output,
        max_connections=connections,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        http2=transport == "h2",
        verify=False,  # test certificate
        timeout=120.0,
        read_timeout=120.0,
        max_retries=1,
        show_progress=False,
        quiet=True,
    )

    started = time.perf_counter()
    try:
        Downloader(config).run()
        elapsed = time.perf_counter() - started
    finally:
        if isinstance(server, H2Server):
            stats = {
                "connections": server.connection_count,
                "peak_streams": server.max_streams_per_connection,
            }
        else:
            stats = {
                "connections": len(
                    {
                        item["client_port"]
                        for item in server.requests
                        if item["range"] and item["range"] != "bytes=0-0"
                    }
                ),
                "peak_streams": 1,
            }
        server.stop()

    assert output.exists(), "download did not produce a file"
    actual = hashlib.sha256(output.read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit(f"checksum mismatch for {transport} -n {connections}")
    output.unlink(missing_ok=True)

    return {
        "elapsed": elapsed,
        "speed": len(data) / elapsed,
        "connections": stats["connections"],
        "peak_streams": stats["peak_streams"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--size", default="16M")
    parser.add_argument("--latency", type=float, default=0.05, help="seconds per request")
    parser.add_argument(
        "--throttle", default="2M", help="per-stream limit (empty/none for unlimited)"
    )
    parser.add_argument("--json", default=None, help="result JSON path")
    args = parser.parse_args(argv)

    size = parse_size(args.size)
    throttle = parse_size(args.throttle) if args.throttle.lower() not in ("", "none") else None
    data = make_content(size, seed=2026)

    cases = [("h1", 1), ("h1", 8), ("h2", 8)]
    results: list[dict] = []
    directory = Path(tempfile.mkdtemp(prefix="pdm-h2-bench-"))

    print(
        f"HTTP/2 multiplexing benchmark: {size / MIB:.0f} MiB, "
        f"{args.latency * 1000:.0f} ms request latency, "
        f"{'unlimited' if throttle is None else f'{throttle / MIB:.1f} MiB/s per stream'}, "
        f"{args.runs} runs (median)\n"
    )
    header = f"{'mode':>10} {'elapsed':>9} {'speed':>12} {'conns':>6} {'streams':>8}"
    print(header)
    print("-" * len(header))

    for transport, connections in cases:
        samples: list[dict] = []
        for _ in range(max(1, args.runs)):
            samples.append(
                run_once(
                    transport=transport,
                    connections=connections,
                    data=data,
                    latency=args.latency,
                    throttle=throttle,
                    directory=directory,
                )
            )
        elapsed = statistics.median(sample["elapsed"] for sample in samples)
        speed = statistics.median(sample["speed"] for sample in samples)
        connections_used = samples[0]["connections"]
        peak = samples[0]["peak_streams"]
        print(
            f"{transport + ' -n ' + str(connections):>10} "
            f"{elapsed:8.2f}s {speed / MIB:9.2f} MiB/s {connections_used:6d} {peak:8d}"
        )
        results.append(
            {
                "mode": f"{transport} -n {connections}",
                "transport": transport,
                "connections_setting": connections,
                "elapsed": round(elapsed, 3),
                "speed": round(speed, 1),
                "samples": round_samples(samples),
                "server_connections": connections_used,
                "peak_streams": peak,
            }
        )

    payload = {
        "date": date.today().isoformat(),
        "size": size,
        "latency": args.latency,
        "throttle": throttle,
        "runs": args.runs,
        "results": results,
    }
    target = (
        Path(args.json)
        if args.json
        else ROOT / "bench" / "results" / f"http2-{date.today().isoformat()}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nraw data: {target}")
    return 0


def round_samples(samples: list[dict]) -> list[dict]:
    return [
        {
            "elapsed": round(sample["elapsed"], 3),
            "speed": round(sample["speed"], 1),
        }
        for sample in samples
    ]


if __name__ == "__main__":
    raise SystemExit(main())
