"""Auto-update: manifest validation, version ordering, verified download, CLI."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

from server import Route, make_content

from personal_download_manager import update
from personal_download_manager.update import (
    UpdateCancelled,
    UpdateCheckError,
    UpdateDownloadError,
    check_update,
    download_update,
    launch_installer,
    parse_manifest,
    version_is_newer,
)


def _manifest(server, package: bytes, version: str = "9.9.9", **extra) -> dict:
    payload = {
        "version": version,
        "url": server.url("/setup.exe"),
        "sha256": hashlib.sha256(package).hexdigest(),
        "size": len(package),
        "notes": "test release",
        "page_url": server.url("/downloads"),
        "published_at": "2026-11-01",
    }
    payload.update(extra)
    return payload


def _serve(server, payload: dict, package: bytes, manifest_path: str = "/latest.json"):
    server.add(manifest_path, Route(content=json.dumps(payload).encode("utf-8")))
    server.add("/setup.exe", Route(content=package))


def test_version_ordering_and_parsing():
    assert version_is_newer("0.2.0", "0.1.9")
    assert version_is_newer("v1.0", "0.9.9")
    assert version_is_newer("1.2.0.1", "1.2.0")
    assert not version_is_newer("1.2", "1.2.0")
    assert not version_is_newer("1.2.0", "1.2")
    assert not version_is_newer("0.1.0", "0.1.0")
    assert not version_is_newer("0.1.0-beta", "0.1.0")
    for bad in ("", "abc", "1.x", "1..2", None):
        with pytest.raises(ValueError):
            update.parse_version(bad)


def test_parse_manifest_validation(server):
    package = b"installer-bytes"
    info = parse_manifest(_manifest(server, package))
    assert info.version == "9.9.9"
    assert info.url.endswith("/setup.exe")
    assert info.size == len(package)
    assert info.sha256 == hashlib.sha256(package).hexdigest()

    base = _manifest(server, package)
    for key in ("version", "url", "sha256"):
        broken = dict(base)
        broken.pop(key)
        with pytest.raises(UpdateCheckError):
            parse_manifest(broken)
    cases = [
        {"version": "not-a-version"},
        {"url": "https://example.com/setup.zip"},
        {"url": "https://user:pass@example.com/setup.exe"},
        {"sha256": "abc"},
        {"size": 0},
        {"size": -1},
        {"notes": 123},
        {"published_at": 123},
        {"page_url": "http://example.com/x"},
    ]
    for patch in cases:
        with pytest.raises(UpdateCheckError):
            parse_manifest(dict(base, **patch))

    with pytest.raises(UpdateCheckError):
        parse_manifest(b"not json")
    with pytest.raises(UpdateCheckError):
        parse_manifest(b"[]")


def test_manifest_requires_https_outside_loopback(server):
    package = b"x"
    payload = _manifest(server, package, url="http://example.com/setup.exe")
    with pytest.raises(UpdateCheckError, match="HTTPS"):
        parse_manifest(payload)
    with pytest.raises(UpdateCheckError, match="HTTPS"):
        check_update("http://example.com/latest.json")


def test_check_and_download_update(server, tmp_path):
    package = make_content(512 * 1024, seed=801)
    payload = _manifest(server, package)
    _serve(server, payload, package)

    info = check_update(server.url("/latest.json"), current_version="0.1.0")
    assert info is not None and info.version == "9.9.9"
    assert check_update(server.url("/latest.json"), current_version="9.9.9") is None

    progress: list[tuple[int, int | None]] = []
    path = download_update(
        info, tmp_path, progress=lambda done, total: progress.append((done, total))
    )
    assert path == tmp_path / "setup.exe"
    assert path.read_bytes() == package
    assert progress and progress[-1][0] == len(package)
    assert not (tmp_path / "setup.exe.part").exists()

    # A second call reuses the verified file without another transfer.
    assert download_update(info, tmp_path) == path


def test_download_rejects_hash_and_size_mismatch(server, tmp_path):
    package = make_content(128 * 1024, seed=802)
    payload = _manifest(server, package, sha256="0" * 64)
    _serve(server, payload, package)
    info = parse_manifest(payload)
    with pytest.raises(UpdateDownloadError, match="SHA-256"):
        download_update(info, tmp_path)
    assert not (tmp_path / "setup.exe").exists()
    assert not (tmp_path / "setup.exe.part").exists()

    payload = _manifest(server, package, size=len(package) + 1)
    _serve(server, payload, package)
    info = parse_manifest(payload)
    with pytest.raises(UpdateDownloadError, match="size"):
        download_update(info, tmp_path)


def test_download_can_be_cancelled(server, tmp_path):
    package = make_content(1024 * 1024, seed=803)
    payload = _manifest(server, package)
    _serve(server, payload, package)
    info = parse_manifest(payload)
    cancel = threading.Event()

    def progress(done, _total):
        cancel.set()

    with pytest.raises(UpdateCancelled):
        download_update(info, tmp_path, progress=progress, cancel_event=cancel)
    assert not (tmp_path / "setup.exe").exists()
    assert not (tmp_path / "setup.exe.part").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="installer launch is Windows-only")
def test_launch_installer_requires_exe_and_calls_popen(tmp_path, monkeypatch):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"MZ")
    calls = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(update.subprocess, "Popen", FakePopen)
    update.launch_installer(installer)
    assert calls and calls[0][0] == [str(installer)]
    assert calls[0][1].get("shell") is False

    text = tmp_path / "setup.txt"
    text.write_bytes(b"x")
    with pytest.raises(update.UpdateError, match="\\.exe"):
        launch_installer(text)


def test_cli_update_json_is_utf8_safe_with_non_ascii_path(server, tmp_path):
    """Windows pipes may use GBK; --json must still be valid UTF-8."""
    import subprocess
    import sys as _sys

    package = make_content(32 * 1024, seed=805)
    payload = _manifest(server, package)
    _serve(server, payload, package)
    target = tmp_path / "更新目录"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            _sys.executable,
            "-m",
            "personal_download_manager",
            "update",
            "--manifest",
            server.url("/latest.json"),
            "--download",
            str(target),
            "--json",
        ],
        cwd=root,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.decode("utf-8"))
    assert data["update"]["version"] == "9.9.9"
    downloaded = Path(data["downloaded"])
    assert downloaded.read_bytes() == package


def test_make_update_manifest_tool(tmp_path):
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "make_update_manifest", root / "packaging" / "make_update_manifest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    installer = tmp_path / "setup-0.2.0.exe"
    installer.write_bytes(b"MZ" + b"x" * 1024)
    out = tmp_path / "latest.json"
    code = module.main(
        [
            "--installer",
            str(installer),
            "--version",
            "0.2.0",
            "--base-url",
            "https://downloads.example.com/personal-download-manager/",
            "--notes",
            "release notes",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["url"] == "https://downloads.example.com/personal-download-manager/setup-0.2.0.exe"
    assert payload["size"] == installer.stat().st_size
    assert payload["sha256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert parse_manifest(payload).version == "0.2.0"


def test_cli_update_check_download_and_launch(server, tmp_path, capsys, monkeypatch):
    from personal_download_manager import cli

    package = make_content(64 * 1024, seed=804)
    payload = _manifest(server, package)
    _serve(server, payload, package)
    manifest = server.url("/latest.json")

    code = cli.main(["update", "--manifest", manifest, "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["current_version"] == "0.1.0"
    assert data["update"]["version"] == "9.9.9"
    assert data["downloaded"] is None

    target = tmp_path / "updates"
    code = cli.main(
        ["update", "--manifest", manifest, "--download", str(target), "--json"]
    )
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    downloaded = Path(data["downloaded"])
    assert downloaded.read_bytes() == package
    assert data["launched"] is False

    launched = []
    monkeypatch.setattr(update, "launch_installer", lambda path: launched.append(path))
    code = cli.main(
        [
            "update",
            "--manifest",
            manifest,
            "--download",
            str(target),
            "--launch",
            "--json",
        ]
    )
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["launched"] is True
    assert launched and Path(launched[0]) == downloaded

    code = cli.main(["update", "--manifest", "http://example.com/latest.json"])
    assert code == 1
    assert "HTTPS" in capsys.readouterr().err
