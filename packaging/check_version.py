"""Fail when the project/package/installer versions disagree.

Sources checked:
  * pyproject.toml           [project].version
  * src/personal_download_manager/__init__.py __version__ = "..."
  * packaging/pdm.iss   default #define AppVersion "..." (CI can override
                             it with ISCC /DAppVersion=...)

The CLI uses ``personal_download_manager.__version__`` at runtime (no separate literal), and
``packaging/make_update_manifest.py`` takes the release version explicitly.

Usage:
    python packaging/check_version.py
    python packaging/check_version.py --expect 0.2.0 --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_INIT_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
_ISS_RE = re.compile(r'^\s*#define\s+AppVersion\s+"([^"]+)"', re.MULTILINE)


def collect_versions(root: Path = ROOT) -> dict[str, str]:
    pyproject = root / "pyproject.toml"
    init = root / "src" / "personal_download_manager" / "__init__.py"
    iss = root / "packaging" / "pdm.iss"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {pyproject}: {exc}") from exc
    try:
        init_text = init.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {init}: {exc}") from exc
    match = _INIT_RE.search(init_text)
    if not match:
        raise ValueError(f"cannot find __version__ in {init}")
    try:
        iss_text = iss.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {iss}: {exc}") from exc
    match_iss = _ISS_RE.search(iss_text)
    if not match_iss:
        raise ValueError(f"cannot find AppVersion in {iss}")
    return {
        "pyproject.toml": str(project),
        "src/personal_download_manager/__init__.py": match.group(1),
        "packaging/pdm.iss": match_iss.group(1),
    }


def check(root: Path = ROOT, expect: str | None = None) -> tuple[str, dict[str, str]]:
    versions = collect_versions(root)
    unique = set(versions.values())
    if len(unique) != 1:
        details = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise ValueError(f"version mismatch: {details}")
    version = unique.pop()
    if expect is not None and version != expect:
        raise ValueError(f"expected version {expect!r}, project has {version!r}")
    return version, versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", default=None, help="required version (e.g. from a tag)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        version, versions = check(args.root, args.expect)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"version": version, "sources": versions}, ensure_ascii=False))
    else:
        print(f"version {version} is consistent across {len(versions)} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
