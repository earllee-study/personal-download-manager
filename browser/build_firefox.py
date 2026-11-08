"""Assemble the Firefox build of the browser extension.

The JavaScript, popup and icons are shared with the Chromium extension; only
the manifest differs (Firefox uses an event page instead of a service worker
and a ``browser_specific_settings.gecko.id`` for native messaging).  This
script copies the shared files next to ``browser/firefox/manifest.json`` so
the folder can be loaded with ``about:debugging`` -> "Load Temporary Add-on".

Usage:
    python browser/build_firefox.py
    python browser/build_firefox.py --check   # verify only, change nothing
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser" / "extension"
FIREFOX = ROOT / "browser" / "firefox"

SHARED = [
    "background.js",
    "mse_bridge.js",
    "mse_capture.js",
    "popup.html",
    "popup.js",
    "icon16.png",
    "icon32.png",
    "icon48.png",
    "icon128.png",
]


def check() -> list[str]:
    """Problems that would stop Firefox from loading the extension."""
    problems: list[str] = []
    manifest_path = FIREFOX / "manifest.json"
    if not manifest_path.exists():
        return [f"missing {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"invalid manifest JSON: {exc}"]

    gecko = (manifest.get("browser_specific_settings") or {}).get("gecko") or {}
    if not gecko.get("id"):
        problems.append("browser_specific_settings.gecko.id is required")
    background = manifest.get("background") or {}
    if "service_worker" in background:
        problems.append("Firefox MV3 does not support background.service_worker")
    if not background.get("scripts"):
        problems.append("background.scripts is required")
    for name in SHARED:
        target = FIREFOX / name
        if not target.exists():
            problems.append(
                f"missing shared file: {name} (run python browser/build_firefox.py)"
            )
        elif target.read_bytes() != (EXTENSION / name).read_bytes():
            problems.append(
                f"stale copy: {name} (run python browser/build_firefox.py)"
            )
    return problems


def build() -> int:
    FIREFOX.mkdir(parents=True, exist_ok=True)
    for name in SHARED:
        source = EXTENSION / name
        if not source.exists():
            print(f"missing source file: {source}", file=sys.stderr)
            return 1
        shutil.copy2(source, FIREFOX / name)
    print(f"built {FIREFOX}")
    problems = check()
    for problem in problems:
        print(f"  problem: {problem}", file=sys.stderr)
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="only verify the Firefox folder"
    )
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for problem in problems:
            print(problem, file=sys.stderr)
        return 0 if not problems else 1
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
