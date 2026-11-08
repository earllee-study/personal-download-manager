"""HTTP/3 under simulated packet loss (QUIC retransmission behaviour).

A plain loopback benchmark cannot show what QUIC is for: there is no loss.
This script puts ``tests/udp_proxy.py`` between the client and the HTTP/3 test
server and drops 0/1/3/5 % of datagrams in both directions, then verifies the
SHA-256 of every download.  Throughput should degrade gracefully while the
bytes stay exact, because QUIC retransmits lost packets instead of failing.

There is no equivalent TCP-loss baseline here: emulating loss on a TCP byte
stream from userspace is not meaningful.  The point is to show that the HTTP/3
path is correct and resilient on a weak link, not that it wins on loopback.

Run from the project root:

    python bench/h3_loss_bench.py
    python bench/h3_loss_bench.py --size 8M --loss 0,0.01,0.03,0.05 --runs 3

Requires aioquic (pip install aioquic).
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

from server import make_content  # noqa: E402

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
    proxy, data: bytes, path: str, expected: str, connections: int, directory: Path
) -> dict:
    output = directory / f"loss-{time.time_ns()}.bin"
    config = DownloadConfig(
        url=proxy.url_for(path),
        output=output,
        http3=True,
        verify=False,
        max_connections=connections,
        min_segment_size=256 * 1024,
        min_split_size=256 * 1024,
        timeout=120.0,
        read_timeout=120.0,
        max_retries=8,
        retry_backoff=0.2,
        show_progress=False,
        quiet=True,
    )
    started = time.perf_counter()
    result = Downloader(config).run()
    elapsed = time.perf_counter() - started
    actual = hashlib.sha256(output.read_bytes()).hexdigest()
    output.unlink(missing_ok=True)
    return {
        "elapsed": elapsed,
        "speed": len(data) / elapsed,
        "retries": result.retries,
        "sha_ok": actual == expected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="4M")
    parser.add_argument(
        "--loss", default="0,0.01,0.03,0.05", help="comma-separated loss ratios"
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--connections", type=int, default=4)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    try:
        from h3server import H3Server
        from udp_proxy import LossyUdpProxy
    except ImportError:
        print("error: aioquic is required (pip install aioquic)", file=sys.stderr)
        return 2

    size = parse_size(args.size)
    losses = [float(item) for item in args.loss.split(",") if item.strip()]
    data = make_content(size, seed=2028)
    expected = hashlib.sha256(data).hexdigest()
    path = "/loss.bin"
    connections = max(1, min(16, args.connections))

    server = H3Server().start()
    server.add(path, data)
    results: list[dict] = []
    directory = Path(tempfile.mkdtemp(prefix="pdm-h3-loss-"))

    print(
        f"HTTP/3 packet loss benchmark: {size / MIB:.1f} MiB, "
        f"{connections} segments on one QUIC connection, "
        f"{args.runs} runs (median)\n"
    )
    header = f"{'loss':>6} {'elapsed':>9} {'speed':>12} {'dropped':>8} {'dropped %':>10} {'sha':>5}"
    print(header)
    print("-" * len(header))
    try:
        for loss in losses:
            proxy = LossyUdpProxy(server.port, loss=loss, seed=99).start()
            samples = []
            try:
                samples = [
                    run_once(
                        proxy, data, path, expected, connections, directory
                    )
                    for _ in range(max(1, args.runs))
                ]
            finally:
                proxy.stop()
            elapsed = statistics.median(sample["elapsed"] for sample in samples)
            speed = statistics.median(sample["speed"] for sample in samples)
            ok = all(sample["sha_ok"] for sample in samples)
            total = proxy.forwarded + proxy.dropped
            dropped_pct = 100.0 * proxy.dropped / total if total else 0.0
            print(
                f"{loss * 100:5.1f}% {elapsed:8.2f}s {speed / MIB:9.2f} MiB/s "
                f"{proxy.dropped:8d} {dropped_pct:9.2f}% {'ok' if ok else 'BAD':>5}"
            )
            results.append(
                {
                    "loss": loss,
                    "elapsed": round(elapsed, 3),
                    "speed": round(speed, 1),
                    "dropped": proxy.dropped,
                    "dropped_pct": round(dropped_pct, 2),
                    "sha_ok": ok,
                    "samples": [
                        {
                            "elapsed": round(sample["elapsed"], 3),
                            "speed": round(sample["speed"], 1),
                            "retries": sample["retries"],
                            "sha_ok": sample["sha_ok"],
                        }
                        for sample in samples
                    ],
                }
            )
    finally:
        server.stop()

    payload = {
        "date": date.today().isoformat(),
        "size": size,
        "connections": connections,
        "runs": args.runs,
        "results": results,
    }
    target = (
        Path(args.json)
        if args.json
        else ROOT
        / "bench"
        / "results"
        / f"http3-loss-{date.today().isoformat()}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nresults -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
