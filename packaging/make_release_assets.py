"""Package release assets from an already-built ``dist`` directory.

Creates the three portable zips, ``SHA256SUMS.txt`` and ``latest.json``.  The
installer must already exist (run ``packaging/build.cmd`` and Inno Setup first).

Usage:
    python packaging/make_release_assets.py --version 0.1.0 ^
        --base-url https://downloads.example.com/personal-download-manager/ ^
        --notes "PDM 0.1.0 rebuild"
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--base-url",
        default="https://downloads.example.com/personal-download-manager/",
    )
    parser.add_argument(
        "--notes",
        default="PDM rebuild (MSE large content, Firefox popup UI, updated Chromium extension)",
    )
    parser.add_argument("--published-at", default=date.today().isoformat())
    args = parser.parse_args(argv)

    dist = args.dist.resolve()
    installer = dist / f"pdm-setup-{args.version}.exe"
    if not installer.is_file():
        print(f"error: installer not found: {installer}", file=sys.stderr)
        return 2

    names = ("pdm-gui", "pdm-cli", "pdm-host")
    missing = [name for name in names if not (dist / name).is_dir()]
    if missing:
        print(f"error: missing dist directories: {', '.join(missing)}", file=sys.stderr)
        return 2

    for name in names:
        zip_base = dist / f"{name}-{args.version}-win64"
        zip_path = zip_base.with_suffix(".zip")
        zip_path.unlink(missing_ok=True)
        # root_dir=dist + base_dir=name keeps the top-level pdm-*/ folder.
        shutil.make_archive(str(zip_base), "zip", root_dir=dist, base_dir=name)

    # Match the historical SHA256SUMS order: cli, gui, host, setup.
    order = [
        dist / f"pdm-cli-{args.version}-win64.zip",
        dist / f"pdm-gui-{args.version}-win64.zip",
        dist / f"pdm-host-{args.version}-win64.zip",
        installer,
    ]
    lines = [f"{sha256_file(path)}  {path.name}" for path in order]
    sums = dist / "SHA256SUMS.txt"
    # Keep LF endings so `sha256sum -c` works under Git Bash/MSYS too.
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    command = [
        sys.executable,
        str(ROOT / "packaging" / "make_update_manifest.py"),
        "--installer",
        str(installer),
        "--version",
        args.version,
        "--base-url",
        args.base_url,
        "--notes",
        args.notes,
        "--published-at",
        args.published_at,
        "--out",
        str(dist / "latest.json"),
    ]
    subprocess.run(command, check=True)

    print()
    print(f"{'file':<38} {'size':>12}  sha256")
    for path in order:
        print(f"{path.name:<38} {path.stat().st_size:>12,}  {sha256_file(path)}")
    print(f"\nwrote {sums} and {dist / 'latest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
