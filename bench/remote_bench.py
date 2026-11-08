"""FTP/SFTP throughput: engine vs raw clients on an unthrottled local link.

FTP can be segmented with REST (``-n``), but a clean loopback has no
per-connection limit: extra connections only add setup/thread overhead.  This
benchmark therefore reports raw ``ftplib``/``paramiko`` clients plus
``PDM ftp -n 1`` (engine overhead) and ``-n 8`` (multi-connection cost),
and ``PDM sftp`` (always single-stream).

This runs local pyftpdlib / paramiko servers with a fixed file, verifies the
SHA-256 of every copy and reports the median of N runs.

    python bench/remote_bench.py
    python bench/remote_bench.py --size 64M --runs 5 --json bench/results/remote.json

pyftpdlib and paramiko are optional test/extra dependencies.
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def run_engine(url: str, output: Path, **overrides) -> float:
    config = DownloadConfig(
        url=url,
        output=output,
        show_progress=False,
        quiet=True,
        timeout=60.0,
        connect_timeout=15.0,
        read_timeout=60.0,
        **overrides,
    )
    started = time.perf_counter()
    Downloader(config).run()
    return time.perf_counter() - started


def run_raw_ftp(url: str, output: Path) -> float:
    import ftplib
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    started = time.perf_counter()
    ftp = ftplib.FTP()
    ftp.connect(parts.hostname, parts.port or 21, timeout=60)
    ftp.login(unquote(parts.username or "anonymous"), unquote(parts.password or ""))
    ftp.voidcmd("TYPE I")
    with open(output, "wb") as handle:
        ftp.retrbinary(f"RETR {unquote(parts.path)}", handle.write, blocksize=64 * 1024)
    ftp.quit()
    return time.perf_counter() - started


def run_raw_sftp(url: str, output: Path, key: Path | None) -> float:
    import paramiko
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    started = time.perf_counter()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {}
    if key is not None:
        kwargs["pkey"] = paramiko.PKey.from_path(str(key))
    else:
        kwargs["password"] = unquote(parts.password or "")
    client.connect(
        parts.hostname,
        parts.port or 22,
        username=unquote(parts.username or ""),
        timeout=60,
        allow_agent=False,
        look_for_keys=False,
        **kwargs,
    )
    try:
        sftp = client.open_sftp()
        sftp.get(unquote(parts.path), str(output))
        sftp.close()
    finally:
        client.close()
    return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="64M")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)

    try:
        from ftpserver import FtpServer
        from sftpserver import SftpServer
    except ImportError as exc:
        print(f"error: benchmarks need pyftpdlib and paramiko: {exc}", file=sys.stderr)
        return 2

    size = parse_size(args.size)
    data = make_content(size, seed=2029)
    expected = hashlib.sha256(data).hexdigest()
    work = Path(tempfile.mkdtemp(prefix="pdm-remote-bench-"))
    remote = work / "remote"
    remote.mkdir()
    (remote / "file.bin").write_bytes(data)

    ftp = FtpServer(remote).start()
    sftp = SftpServer(remote).start()
    results: list[dict] = []
    try:
        cases = [
            ("ftp raw ftplib", lambda out: run_raw_ftp(ftp.url("/file.bin"), out), None),
            (
                "PDM ftp -n 1",
                lambda out: run_engine(
                    ftp.url("/file.bin"), out, max_connections=1
                ),
                None,
            ),
            (
                "PDM ftp -n 8",
                lambda out: run_engine(
                    ftp.url("/file.bin"), out, max_connections=8
                ),
                None,
            ),
            (
                "sftp raw paramiko",
                lambda out: run_raw_sftp(sftp.url("/file.bin"), out, None),
                None,
            ),
            (
                "PDM sftp",
                lambda out: run_engine(
                    sftp.url("/file.bin"), out, sftp_auto_host_key=True, verify=False
                ),
                None,
            ),
        ]
        print(
            f"FTP/SFTP throughput: {size / MIB:.0f} MiB, local servers, "
            f"{args.runs} runs (median)\n"
        )
        header = f"{'mode':>20} {'elapsed':>9} {'speed':>12} {'sha':>5}"
        print(header)
        print("-" * len(header))
        for label, runner, _ in cases:
            samples = []
            for index in range(max(1, args.runs)):
                output = work / f"out-{len(results)}-{index}.bin"
                elapsed = runner(output)
                assert output.exists(), f"{label} produced no file"
                assert sha256(output) == expected, f"{label} checksum mismatch"
                samples.append(elapsed)
                output.unlink(missing_ok=True)
            elapsed = statistics.median(samples)
            speed = size / elapsed
            print(
                f"{label:>20} {elapsed:8.2f}s {speed / MIB:9.2f} MiB/s "
                f"{'ok':>5}"
            )
            results.append(
                {
                    "mode": label,
                    "elapsed": round(elapsed, 3),
                    "speed_mib": round(speed / MIB, 2),
                    "samples": [round(sample, 3) for sample in samples],
                    "sha_ok": True,
                }
            )
    finally:
        ftp.stop()
        sftp.stop()

    payload = {
        "date": date.today().isoformat(),
        "size": size,
        "runs": args.runs,
        "results": results,
    }
    target = (
        Path(args.json)
        if args.json
        else ROOT / "bench" / "results" / f"remote-{date.today().isoformat()}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nresults -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
