"""Register the PDM native messaging host with Chromium browsers and Firefox.

This only writes HKCU keys and manifest files under
``%LOCALAPPDATA%\\PersonalDownloadManager\\native-host``; it does not install the browser
extension (load ``browser/extension`` unpacked in chrome://extensions /
edge://extensions, or ``browser/firefox`` in about:debugging).

Usage:
    python packaging/install_native_host.py
    python packaging/install_native_host.py --browsers chrome,edge
    python packaging/install_native_host.py --browsers firefox
    python packaging/install_native_host.py --uninstall
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from personal_download_manager.browser_host import (  # noqa: E402
    extension_id_from_key,
    firefox_native_manifest,
    native_manifest,
)

HOST_NAME = "com.pdm.downloader.host"
BROWSER_KEYS = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "chromium": r"Software\Chromium\NativeMessagingHosts",
    "firefox": r"Software\Mozilla\NativeMessagingHosts",
}
CHROMIUM_BROWSERS = {"chrome", "edge", "chromium"}


def install_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / "PersonalDownloadManager" / "native-host"


def default_host_exe() -> Path:
    return ROOT / "dist" / "pdm-host" / "pdm-host.exe"


def extension_id() -> str:
    manifest = json.loads(
        (ROOT / "browser" / "extension" / "manifest.json").read_text(encoding="utf-8")
    )
    return extension_id_from_key(manifest["key"])


def firefox_extension_id() -> str:
    manifest = json.loads(
        (ROOT / "browser" / "firefox" / "manifest.json").read_text(encoding="utf-8")
    )
    gecko = (manifest.get("browser_specific_settings") or {}).get("gecko") or {}
    return str(gecko.get("id") or "pdm@local")


def install(host: Path, browsers: list[str], ext_id: str) -> int:
    if os.name != "nt":
        print("native messaging registration is Windows-only", file=sys.stderr)
        return 1
    if not host.exists():
        print(
            f"host executable not found: {host}\n"
            "Build it first: python -m PyInstaller --distpath dist "
            "--workpath build packaging/pdm-host.spec",
            file=sys.stderr,
        )
        return 1

    import winreg

    target_dir = install_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    # The bundle is onedir: the executable needs its _internal directory, so
    # install the whole folder rather than copying just the exe.
    shutil.copytree(host.parent, target_dir, dirs_exist_ok=True)
    target_exe = target_dir / host.name

    chromium = [name for name in browsers if name in CHROMIUM_BROWSERS]
    manifests: dict[str, Path] = {}
    if chromium:
        manifest_path = target_dir / f"{HOST_NAME}.json"
        manifest_path.write_text(
            json.dumps(
                native_manifest(HOST_NAME, str(target_exe), [ext_id]),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        manifests["chromium"] = manifest_path

    if "firefox" in browsers:
        ff_id = firefox_extension_id()
        ff_manifest = target_dir / f"{HOST_NAME}.firefox.json"
        ff_manifest.write_text(
            json.dumps(
                firefox_native_manifest(HOST_NAME, str(target_exe), [ff_id]),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        manifests["firefox"] = ff_manifest

    for browser in browsers:
        browser_manifest = (
            manifests["firefox"] if browser == "firefox" else manifests["chromium"]
        )
        key = f"{BROWSER_KEYS[browser]}\\{HOST_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, "", 0, winreg.REG_SZ, str(browser_manifest))
        print(f"registered {browser}: HKCU\\{key}")

    if chromium:
        print(f"chrome id    : {ext_id}")
    if "firefox" in browsers:
        print(f"firefox id   : {firefox_extension_id()}")
    for label, path in manifests.items():
        print(f"{label:<12} : {path}")
    print(f"host         : {target_exe}")
    if chromium:
        print(
            "\nnext: open chrome://extensions (or edge://extensions), enable "
            "Developer mode, and load the unpacked folder browser/extension"
        )
    if "firefox" in browsers:
        print(
            "next: run python browser/build_firefox.py, then in Firefox open "
            "about:debugging -> This Firefox -> Load Temporary Add-on and pick "
            "browser/firefox/manifest.json (or install the folder as an "
            "unsigned add-on via about:config xpinstall.signatures.required)."
        )
    return 0


def uninstall(browsers: list[str]) -> int:
    if os.name != "nt":
        print("native messaging registration is Windows-only", file=sys.stderr)
        return 1
    import winreg

    for browser in browsers:
        key = f"{BROWSER_KEYS[browser]}\\{HOST_NAME}"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            print(f"removed {browser} registration")
        except FileNotFoundError:
            print(f"{browser}: no registration found")
    for name in (f"{HOST_NAME}.json", f"{HOST_NAME}.firefox.json"):
        (install_dir() / name).unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        type=Path,
        default=None,
        help="path to pdm-host.exe (default: dist/pdm-host)",
    )
    parser.add_argument(
        "--browsers",
        default="chrome,edge",
        help="comma-separated: chrome,edge,chromium,firefox",
    )
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)

    browsers = [item.strip().lower() for item in args.browsers.split(",") if item.strip()]
    unknown = [item for item in browsers if item not in BROWSER_KEYS]
    if unknown:
        print(f"unknown browser(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not browsers:
        print("no browsers selected", file=sys.stderr)
        return 2

    if args.uninstall:
        return uninstall(browsers)
    return install(args.host or default_host_exe(), browsers, extension_id())


if __name__ == "__main__":
    raise SystemExit(main())
