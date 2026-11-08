"""HTTP/1.1 vs HTTP/2 vs HTTP/3 (QUIC) with per-stream throttling.

All three servers are local and TLS-capable, add the same request latency and
limit every stream to the same rate.  On a clean loopback link QUIC has no
packet loss to recover from, so the interesting, reproducible result is
connection/stream behaviour: h3 carries every segment on one QUIC connection
(one UDP socket) just like h2 does on one TCP connection, and h1 needs one
TCP/TLS connection per segment.

Run from the project root:

    python bench/http3_bench.py
    python bench/http3_bench.py --runs 5 --size 32M --latency 0.1 --throttle 2M

aioquic is required for the h3 case; install with ``pip install aioquic``.
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


def _make_server(transport: str, data: bytes, path: str, latency: float, throttle):
    if transport == "h3":
        try:
            from h3server import H3Server
        except ImportError as exc:  # pragma: no cover - aioquic missing
            raise SystemExit(
                "HTTP/3 benchmark needs aioquic: pip install aioquic"
            ) from exc
        server = H3Server(latency=latency, throttle_bps=throttle).start()
        server.add(path, data)
        return server
    if transport == "h2":
        from h2server import H2Server

        server = H2Server(latency=latency, throttle_bps=throttle).start()
        server.add(path, data)
        return server
    server = TestServer(tls=True, latency=latency).start()
    server.add(path, Route(content=data, throttle_bps=throttle))
    return server


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
    server = _make_server(transport, data, path, latency, throttle)
    output = directory / name
    config = DownloadConfig(
        url=server.url_for(path) if transport == "h3" else server.url(path),
        output=output,
        max_connections=connections,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        http2=transport == "h2",
        http3=transport == "h3",
        verify=False,  # test certificate
        timeout=180.0,
        read_timeout=180.0,
        max_retries=1,
        show_progress=False,
        quiet=True,
    )

    started = time.perf_counter()
    try:
        Downloader(config).run()
        elapsed = time.perf_counter() - started
    finally:
        if transport in ("h2", "h3"):
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


def round_samples(samples: list[dict]) -> list[dict]:
    return [
        {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in sample.items()
        }
        for sample in samples
    ]


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
    throttle = (
        parse_size(args.throttle)
        if args.throttle.lower() not in ("", "none")
        else None
    )
    data = make_content(size, seed=2027)

    cases = [("h1", 8), ("h2", 8), ("h3", 8), ("h3", 16)]
    results: list[dict] = []
    directory = Path(tempfile.mkdtemp(prefix="pdm-h3-bench-"))

    print(
        f"HTTP/3 benchmark: {size / MIB:.0f} MiB, "
        f"{args.latency * 1000:.0f} ms request latency, "
        f"{'unlimited' if throttle is None else f'{throttle / MIB:.1f} MiB/s per stream'}, "
        f"{args.runs} runs (median)\n"
    )
    header = f"{'mode':>10} {'elapsed':>9} {'speed':>12} {'conns':>6} {'streams':>8}"
    print(header)
    print("-" * len(header))

    for transport, connections in cases:
        samples = [
            run_once(
                transport=transport,
                connections=connections,
                data=data,
                latency=args.latency,
                throttle=throttle,
                directory=directory,
            )
            for _ in range(max(1, args.runs))
        ]
        elapsed = statistics.median(sample["elapsed"] for sample in samples)
        speed = statistics.median(sample["speed"] for sample in samples)
        used = samples[0]["connections"]
        peak = samples[0]["peak_streams"]
        print(
            f"{transport + ' -n ' + str(connections):>10} "
            f"{elapsed:8.2f}s {speed / MIB:9.2f} MiB/s {used:6d} {peak:8d}"
        )
        results.append(
            {
                "mode": f"{transport} -n {connections}",
                "transport": transport,
                "connections_setting": connections,
                "elapsed": round(elapsed, 3),
                "speed": round(speed, 1),
                "samples": round_samples(samples),
                "server_connections": used,
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
        else ROOT / "bench" / "results" / f"http3-{date.today().isoformat()}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nresults -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
