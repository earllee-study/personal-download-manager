"""Generate and validate an update manifest from a built installer.

Usage:
    python packaging/make_update_manifest.py ^
        --installer dist\\pdm-setup-0.2.0.exe ^
        --version 0.2.0 ^
        --base-url https://downloads.example.com/personal-download-manager/ ^
        --notes "Bug fixes" ^
        --out packaging/update-manifest.json

The script computes size and SHA-256, then runs the same validator the client
uses (personal_download_manager.update.parse_manifest) so a typo cannot ship a broken manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from personal_download_manager.update import parse_manifest  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", default=None, help="directory URL ending with /")
    parser.add_argument("--url", default=None, help="exact installer URL")
    parser.add_argument("--notes", default="")
    parser.add_argument("--notes-file", default=None, type=Path)
    parser.add_argument("--page-url", default=None)
    parser.add_argument("--published-at", default=date.today().isoformat())
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    installer = args.installer
    if not installer.is_file():
        print(f"error: installer not found: {installer}", file=sys.stderr)
        return 2
    if installer.suffix.lower() != ".exe":
        print("error: installer must be a .exe", file=sys.stderr)
        return 2
    if args.url:
        url = args.url
    elif args.base_url:
        url = args.base_url.rstrip("/") + "/" + quote(installer.name)
    else:
        print("error: pass --url or --base-url", file=sys.stderr)
        return 2

    notes = args.notes
    if args.notes_file:
        try:
            notes = args.notes_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read notes: {exc}", file=sys.stderr)
            return 2

    payload = {
        "version": args.version,
        "url": url,
        "sha256": sha256_file(installer),
        "size": installer.stat().st_size,
        "notes": notes,
        "page_url": args.page_url,
        "published_at": args.published_at,
    }
    try:
        parse_manifest(payload)
    except Exception as exc:  # UpdateCheckError
        print(f"error: invalid manifest: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out}")
    print(f"  version: {payload['version']}")
    print(f"  size:    {payload['size']} bytes")
    print(f"  sha256:  {payload['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
