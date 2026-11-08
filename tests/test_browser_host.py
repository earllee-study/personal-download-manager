"""Native messaging framing, capture files and the extension manifest."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import sys
from pathlib import Path

import pytest

from personal_download_manager import browser_host
from personal_download_manager.browser_host import (
    MAX_MSE_TOTAL,
    MAX_MSE_TRACK,
    MseSessions,
    extension_id_from_key,
    firefox_native_manifest,
    handle_message,
    main,
    native_manifest,
    read_message,
    write_message,
)
from personal_download_manager.gui.capture import Capture, load_captures, make_task
from personal_download_manager.gui.settings import AppSettings

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ID = "hfiihggbmdmoofcadgmkingenjgcaend"


# ----------------------------------------------------------------------
# framing
# ----------------------------------------------------------------------
def test_framing_roundtrip():
    stream = io.BytesIO()
    write_message(stream, {"action": "ping", "text": "你好"})
    stream.seek(0)
    assert read_message(stream) == {"action": "ping", "text": "你好"}
    assert read_message(stream) is None


def test_framing_rejects_bad_messages():
    with pytest.raises(ValueError):
        read_message(io.BytesIO(struct.pack("<I", 8 * 1024 * 1024 + 1)))
    with pytest.raises(ValueError):
        read_message(io.BytesIO(struct.pack("<I", 4) + b"{bad"))
    stream = io.BytesIO(struct.pack("<I", 4) + b"null")
    with pytest.raises(ValueError):
        read_message(stream)


# ----------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------
def test_ping(tmp_path):
    assert handle_message({"action": "ping"}, tmp_path)["ok"] is True


def test_download_writes_capture(tmp_path):
    reply = handle_message(
        {
            "action": "download",
            "url": "https://example.com/file.zip",
            "filename": r"../evil\file.zip",
            "referer": "https://example.com/",
            "cookie": "a=b; c=d",
            "user_agent": "UA/1.0",
        },
        tmp_path,
    )
    assert reply == {"ok": True, "queued": True}
    files = list(tmp_path.glob("capture-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["url"] == "https://example.com/file.zip"
    assert data["cookie"] == "a=b; c=d"
    assert data["referer"] == "https://example.com/"
    # The host itself does not sanitize paths; the GUI does (see below).
    assert data["filename"] == r"../evil\file.zip"


def test_hls_and_dash_capture_kinds(tmp_path):
    def capture_for(url):
        for path in tmp_path.glob("capture-*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["url"] == url:
                return data
        raise AssertionError(f"no capture for {url}")

    reply = handle_message(
        {"action": "download", "url": "https://cdn.test/video/index.m3u8", "kind": "hls"},
        tmp_path,
    )
    assert reply == {"ok": True, "queued": True}
    assert capture_for("https://cdn.test/video/index.m3u8")["hls"] is True

    # The .m3u8 suffix alone is enough to route to the HLS engine.
    handle_message(
        {"action": "download", "url": "https://cdn.test/other.m3u8"}, tmp_path
    )
    assert capture_for("https://cdn.test/other.m3u8")["hls"] is True

    # DASH is accepted both by explicit kind and by the .mpd suffix.
    for message in (
        {"action": "download", "url": "https://cdn.test/video/manifest.mpd"},
        {"action": "download", "url": "https://cdn.test/dash-by-kind", "kind": "dash"},
    ):
        reply = handle_message(message, tmp_path)
        assert reply == {"ok": True, "queued": True}
        data = capture_for(message["url"])
        assert data["dash"] is True
        assert data["hls"] is False


def test_download_rejects_non_http(tmp_path):
    for url in ("ftp://example.com/f", "file:///C:/x", "javascript:alert(1)", ""):
        reply = handle_message({"action": "download", "url": url}, tmp_path)
        assert reply["ok"] is False, url
    assert not list(tmp_path.glob("capture-*.json"))


def test_unknown_action(tmp_path):
    reply = handle_message({"action": "rm -rf"}, tmp_path)
    assert reply["ok"] is False
    assert "unknown" in reply["error"]


def test_main_loop_handles_multiple_messages(tmp_path, monkeypatch):
    payload = io.BytesIO()
    write_message(payload, {"action": "ping"})
    write_message(payload, {"action": "download", "url": "https://example.com/a.bin"})
    payload.seek(0)
    output = io.BytesIO()

    class FakeIn:
        buffer = payload

    class FakeOut:
        buffer = output

    monkeypatch.setattr(sys, "stdin", FakeIn())
    monkeypatch.setattr(sys, "stdout", FakeOut())
    assert main(["--spool", str(tmp_path)]) == 0

    output.seek(0)
    first = read_message(output)
    second = read_message(output)
    assert first["ok"] is True and first["version"]
    assert second == {"ok": True, "queued": True}
    assert len(list(tmp_path.glob("capture-*.json"))) == 1


# ----------------------------------------------------------------------
# extension manifest
# ----------------------------------------------------------------------
def test_extension_id_matches_manifest_key():
    manifest = json.loads(
        (ROOT / "browser" / "extension" / "manifest.json").read_text(encoding="utf-8")
    )
    assert extension_id_from_key(manifest["key"]) == EXTENSION_ID
    assert manifest["manifest_version"] == 3
    assert "nativeMessaging" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]


def test_native_manifest_shape(tmp_path):
    manifest = native_manifest(
        "com.pdm.downloader.host", str(tmp_path / "host.exe"), [EXTENSION_ID]
    )
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID}/"]
    assert Path(manifest["path"]).is_absolute()


# ----------------------------------------------------------------------
# capture files -> tasks
# ----------------------------------------------------------------------
def test_load_captures_skips_and_removes_garbage(tmp_path):
    good = tmp_path / "capture-good.json"
    good.write_text(
        json.dumps({"url": "https://example.com/a.bin", "filename": "a.bin"}),
        encoding="utf-8",
    )
    bad = tmp_path / "capture-bad.json"
    bad.write_text("{not json", encoding="utf-8")
    ftp = tmp_path / "capture-ftp.json"
    ftp.write_text(json.dumps({"url": "ftp://example.com/a"}), encoding="utf-8")

    captures = load_captures(tmp_path)
    assert [capture.url for _path, capture in captures] == ["https://example.com/a.bin"]
    assert not bad.exists()
    assert not ftp.exists()


def test_make_task_sanitizes_and_numbers(tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    existing = tmp_path / "file.bin"
    existing.write_bytes(b"x")

    capture = Capture(
        url="https://example.com/file.bin",
        filename="../evil\\file.bin",
        referer="https://example.com/",
        cookie="a=b",
        user_agent="UA",
    )
    task = make_task(capture, tmp_path, settings)
    assert task.filename == "file (1).bin"  # same convention as unique_path
    assert task.options["referer"] == "https://example.com/"
    assert task.options["cookie"] == "a=b"
    assert task.options["user_agent"] == "UA"

    reserved = {task.path}
    second = make_task(capture, tmp_path, settings, reserved)
    assert second.path not in reserved
    assert second.filename != task.filename


def test_hls_capture_builds_hls_task(tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    capture = Capture(
        url="https://cdn.test/vod/master.m3u8",
        filename="master.m3u8",
        hls=True,
    )
    task = make_task(capture, tmp_path, settings)
    assert task.options["hls"] is True
    assert task.options["hls_quality"] == "best"
    assert task.filename.endswith(".ts")


# ----------------------------------------------------------------------
# Firefox support
# ----------------------------------------------------------------------
def test_firefox_native_manifest_uses_allowed_extensions(tmp_path):
    manifest = firefox_native_manifest(
        "com.pdm.downloader.host", str(tmp_path / "host.exe"), ["pdm@local"]
    )
    assert manifest["type"] == "stdio"
    assert manifest["allowed_extensions"] == ["pdm@local"]
    assert "allowed_origins" not in manifest
    assert Path(manifest["path"]).is_absolute()


def test_firefox_extension_manifest_is_an_mv3_event_page():
    manifest = json.loads(
        (ROOT / "browser" / "firefox" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["manifest_version"] == 3
    gecko = manifest["browser_specific_settings"]["gecko"]
    assert gecko["id"] == "pdm@local"
    assert manifest["background"] == {"scripts": ["background.js"]}
    assert "nativeMessaging" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]


def test_firefox_folder_is_built_from_the_shared_sources():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_firefox", ROOT / "browser" / "build_firefox.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    problems = module.check()
    assert problems == [], problems


def test_installer_knows_the_firefox_registry_key():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "install_native_host", ROOT / "packaging" / "install_native_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (
        module.BROWSER_KEYS["firefox"]
        == r"Software\Mozilla\NativeMessagingHosts"
    )
    assert module.firefox_extension_id() == "pdm@local"


def test_background_script_covers_both_takeover_paths():
    text = (ROOT / "browser" / "extension" / "background.js").read_text(
        encoding="utf-8"
    )
    assert "downloads.onDeterminingFilename" in text   # Chromium
    assert "downloads.onCreated" in text               # Firefox
    assert "chrome.storage.session || chrome.storage.local" in text


def test_extension_page_scan_and_dash_handoff():
    background = (ROOT / "browser" / "extension" / "background.js").read_text(
        encoding="utf-8"
    )
    popup = (ROOT / "browser" / "extension" / "popup.js").read_text(encoding="utf-8")
    html = (ROOT / "browser" / "extension" / "popup.html").read_text(encoding="utf-8")
    assert "pageMediaScan" in background
    assert "scanPage" in background
    assert "chrome.scripting" in background
    assert "allFrames: true" in background
    assert "shadowRoot" in background
    assert "blob:" in background
    assert "parseMasterPlaylist" in background
    assert "expandHlsMaster" in background
    assert "sources" in background and "label" in background
    assert "scanPage" in popup
    assert 'id="scan"' in html
    assert 'id="mseHint"' in html
    assert "DASH 清单" in popup
    assert "DASH（暂不支持）" not in popup


def test_demo_server_hls_and_dash_routes_are_consistent():
    import importlib.util

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from personal_download_manager.hls import parse_playlist

    spec = importlib.util.spec_from_file_location(
        "serve_demo", ROOT / "browser" / "serve_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    media = parse_playlist(
        module.PLAYLIST.decode(), "http://127.0.0.1:8000/media/main.m3u8"
    ).media
    assert media is not None and len(media.segments) == len(module.SEGMENTS)
    key = module.ROUTES["/media/key.bin"][0]
    for index, segment in enumerate(media.segments):
        ciphertext = module.ROUTES[f"/media/seg{index}.ts"][0]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(module.IV)).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        plain = unpadder.update(decryptor.update(ciphertext))
        plain += unpadder.update(decryptor.finalize()) + unpadder.finalize()
        assert plain == module.SEGMENTS[index]

    assert b"<MPD" in module.ROUTES["/media/other.mpd"][0]
    assert len(module.ROUTES["/media/plain.mp4"][0]) == 256 * 1024
    assert module.ROUTES["/media/mse-demo.mp4"][0][4:8] == b"ftyp"
    assert module.ROUTES["/media/mse-video.mp4"][0][4:8] == b"ftyp"
    assert module.ROUTES["/media/mse-audio.mp4"][0][4:8] == b"ftyp"
    assert len(module.EXPECTED_HLS) == 3 * 256 * 1024
    assert len(module.ROUTES["/media/takeover.bin"][0]) == 128 * 1024
    master = module.ROUTES["/media/master.m3u8"][0]
    assert b"1280x720" in master and b"640x360" in master
    low = parse_playlist(
        module.ROUTES["/media/low.m3u8"][0].decode(),
        "http://127.0.0.1:8000/media/low.m3u8",
    ).media
    assert low is not None and len(low.segments) == 1
    page = (ROOT / "browser" / "test-page.html").read_text(encoding="utf-8")
    assert "takeover.bin" in page and 'download="takeover-demo.bin"' in page
    assert "master.m3u8" in page


def test_browser_scan_test_page_covers_frames_and_shadow_dom():
    page = (ROOT / "browser" / "test-page.html").read_text(encoding="utf-8")
    frame = (ROOT / "browser" / "frame.html").read_text(encoding="utf-8")
    assert "scripted.m3u8" in page
    assert "attachShadow" in page
    assert "shadow.m3u8" in page
    assert "blob:" in page
    assert "browser/frame.html" in page
    assert "iframe.m3u8" in frame
    assert "cookie-check.bin" in page and "pdmcookie=verified" in page
    assert "takeover.bin" in page


def test_extension_javascript_has_valid_syntax():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for name in (
        "background.js",
        "mse_bridge.js",
        "mse_capture.js",
        "popup.js",
    ):
        result = subprocess.run(
            [node, "--check", str(ROOT / "browser" / "extension" / name)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr


def test_dash_capture_builds_a_dash_task(tmp_path):
    from personal_download_manager.gui.capture import Capture, make_task

    settings = AppSettings(download_dir=str(tmp_path))
    capture = Capture(
        url="https://cdn.test/vod/manifest.mpd",
        filename="manifest.mpd",
        dash=True,
    )
    task = make_task(capture, tmp_path, settings)
    assert task.options["dash"] is True
    assert task.options["hls"] if "hls" in task.options else True
    assert task.options["dash_track"] == "video"
    assert task.output.endswith(".mp4")


# ----------------------------------------------------------------------
# experimental MSE capture (bounded, single track)
# ----------------------------------------------------------------------
def _mse_start(
    tmp_path,
    mime="video/mp4",
    filename="capture.mp4",
    kind="video",
    track=None,
):
    manager = MseSessions(tmp_path)
    message = {
        "action": "mse_start",
        "mime": mime,
        "filename": filename,
        "kind": kind,
    }
    if track:
        message["track"] = track
    reply = handle_message(message, tmp_path, mse_sessions=manager)
    assert reply["ok"] is True, reply
    return manager, reply["session"]


def _mse_chunk(manager, session, payload, seq, tmp_path):
    return handle_message(
        {
            "action": "mse_chunk",
            "session": session,
            "seq": seq,
            "data": base64.b64encode(payload).decode(),
        },
        tmp_path,
        mse_sessions=manager,
    )


def test_mse_capture_chunks_finalize_to_fmp4(tmp_path):
    manager, session = _mse_start(tmp_path)
    first = b"\x00\x00\x00\x18ftypmp42INIT"
    second = b"\x00\x00\x00\x10moofMEDIA"
    for seq, chunk in enumerate((first, second)):
        reply = handle_message(
            {
                "action": "mse_chunk",
                "session": session,
                "seq": seq,
                "data": base64.b64encode(chunk).decode(),
            },
            tmp_path,
            mse_sessions=manager,
        )
        assert reply["ok"] is True, reply
        assert reply["bytes"] == len(first) + (len(second) if seq else 0)

    reply = handle_message(
        {"action": "mse_end", "session": session},
        tmp_path,
        mse_sessions=manager,
    )
    assert reply["ok"] is True, reply
    output = Path(reply["path"])
    assert output.read_bytes() == first + second
    assert reply["size"] == len(first) + len(second)
    assert reply["sha256"] == hashlib.sha256(first + second).hexdigest()
    assert not list((tmp_path / "mse").glob("*.part"))


def test_mse_rejects_drm_mime_and_bad_sequences(tmp_path):
    manager = MseSessions(tmp_path)
    for mime in ("video/mp4; codecs=encv", "audio/mp4; codecs=enca"):
        reply = handle_message(
            {"action": "mse_start", "mime": mime, "kind": "video"},
            tmp_path,
            mse_sessions=manager,
        )
        assert reply["ok"] is False
        assert "DRM" in reply["error"]

    manager, session = _mse_start(tmp_path)
    payload = base64.b64encode(b"abc").decode()
    bad = handle_message(
        {"action": "mse_chunk", "session": session, "seq": 1, "data": payload},
        tmp_path,
        mse_sessions=manager,
    )
    assert bad["ok"] is False
    assert "sequence" in bad["error"]
    # The expected first sequence still works.
    good = handle_message(
        {"action": "mse_chunk", "session": session, "seq": 0, "data": payload},
        tmp_path,
        mse_sessions=manager,
    )
    assert good["ok"] is True


def test_mse_limits_abort_and_filename_sanitizing(tmp_path):
    manager, session = _mse_start(
        tmp_path, filename=r"..\..\evil\clip?.mp4"
    )
    # Path traversal is removed and the session stays inside the spool.
    state = manager.sessions[session]
    assert state["part"].parent == tmp_path / "mse"
    assert state["filename"].endswith(".mp4")
    assert ".." not in state["filename"] and "?" not in state["filename"]

    too_big = base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode()
    reply = handle_message(
        {"action": "mse_chunk", "session": session, "seq": 0, "data": too_big},
        tmp_path,
        mse_sessions=manager,
    )
    assert reply["ok"] is False
    assert "4 MiB" in reply["error"]

    handle_message(
        {
            "action": "mse_abort",
            "session": session,
        },
        tmp_path,
        mse_sessions=manager,
    )
    assert not state["part"].exists()


def test_mse_end_without_data_fails_cleanly(tmp_path):
    manager, session = _mse_start(tmp_path)
    reply = handle_message(
        {"action": "mse_end", "session": session},
        tmp_path,
        mse_sessions=manager,
    )
    assert reply["ok"] is False
    assert "no MSE data" in reply["error"]


def test_mse_extension_wiring_and_fixture(tmp_path):
    background = (ROOT / "browser" / "extension" / "background.js").read_text(
        encoding="utf-8"
    )
    popup = (ROOT / "browser" / "extension" / "popup.js").read_text(encoding="utf-8")
    html = (ROOT / "browser" / "extension" / "popup.html").read_text(encoding="utf-8")
    capture = (ROOT / "browser" / "extension" / "mse_capture.js").read_text(
        encoding="utf-8"
    )
    bridge = (ROOT / "browser" / "extension" / "mse_bridge.js").read_text(
        encoding="utf-8"
    )
    for text in (background, popup, capture, bridge):
        assert "mse" in text.lower()
    assert "mseStart" in background and "mseStop" in background
    assert "mse_capture.js" in background
    assert "mse_bridge.js" in background
    assert 'id="mseStart"' in html and 'id="mseStop"' in html
    assert "requestMediaKeySystemAccess" in capture
    assert "appendBuffer" in capture
    # The host enforces configurable limits; the hook must not hard-cap chunks.
    assert "MAX_TRACK" not in capture
    assert "MAX_TOTAL" not in capture
    assert "low disk space" in background
    assert "trackByBuffer" in capture and "sniffKind" in capture
    assert "changeType" in capture and "track-${" in capture
    assert "mse_end_all" in background and "mse_abort_all" in background
    assert "mseState.tracks" in background and "merged" in background
    # Firefox cannot read page-principal ArrayBuffers from a content script;
    # Gecko gets base64 strings from the MAIN world, Chromium keeps zero-copy.
    assert "const GECKO = /firefox/i.test" in capture
    assert 'encoding: "base64"' in capture
    assert "[bytes.buffer]" in capture
    assert 'typeof event.data.data === "string"' in bridge
    assert "Firefox 128" in background
    assert "native host timed out" in background and "timeoutAfter" in background
    assert "Promise.race" in background
    assert "mseEvent" in bridge and "track" in bridge
    assert "merged" in popup and "tracks" in popup
    assert "sendRuntimeMessage" in popup and "宿主响应超时" in popup
    fixture = ROOT / "browser" / "mse-fixture.mp4"
    audio = ROOT / "browser" / "mse-audio.mp4"
    assert fixture.exists() and fixture.stat().st_size > 0
    assert audio.exists() and audio.stat().st_size > 0
    page = (ROOT / "browser" / "mse-test.html").read_text(encoding="utf-8")
    assert "appendBuffer" in page and "mse-demo.mp4" in page
    assert "mse-video.mp4" in page and "mse-audio.mp4" in page
    assert "playMulti" in page and "addSourceBuffer" in page


def test_mse_multi_track_end_all_keeps_tracks_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_host, "_configured_ffmpeg", lambda: None)
    manager = MseSessions(tmp_path)
    video = handle_message(
        {
            "action": "mse_start",
            "mime": 'video/mp4; codecs="avc1"',
            "filename": "clip.mp4",
            "kind": "video",
            "track": "track-1",
        },
        tmp_path,
        mse_sessions=manager,
    )
    audio = handle_message(
        {
            "action": "mse_start",
            "mime": 'audio/mp4; codecs="mp4a"',
            "filename": "clip.mp4",
            "kind": "audio",
            "track": "track-2",
        },
        tmp_path,
        mse_sessions=manager,
    )
    assert video["ok"] is True and audio["ok"] is True, (video, audio)
    assert _mse_chunk(manager, video["session"], b"VIDEO", 0, tmp_path)["ok"] is True
    assert _mse_chunk(manager, audio["session"], b"AUDIO", 0, tmp_path)["ok"] is True

    reply = handle_message(
        {"action": "mse_end_all"}, tmp_path, mse_sessions=manager
    )
    assert reply["ok"] is True, reply
    assert reply["track_count"] == 2
    assert {item["track"] for item in reply["files"]} == {"track-1", "track-2"}
    assert {item["kind"] for item in reply["files"]} == {"video", "audio"}
    assert reply["merged"] is None
    assert "ffmpeg not found" in reply["merge_error"]
    for item in reply["files"]:
        assert Path(item["path"]).is_file()
    assert reply["path"] == reply["files"][0]["path"]


def test_mse_multi_track_merges_with_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_host, "_configured_ffmpeg", lambda: "ffmpeg-fake")
    commands = []

    def fake_run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"MERGED")

    monkeypatch.setattr(browser_host, "_run_ffmpeg", fake_run)
    manager = MseSessions(tmp_path)
    video = handle_message(
        {
            "action": "mse_start",
            "mime": "video/mp4",
            "filename": "clip.mp4",
            "kind": "video",
            "track": "v",
        },
        tmp_path,
        mse_sessions=manager,
    )
    audio = handle_message(
        {
            "action": "mse_start",
            "mime": "audio/mp4",
            "filename": "clip.mp4",
            "kind": "audio",
            "track": "a",
        },
        tmp_path,
        mse_sessions=manager,
    )
    _mse_chunk(manager, video["session"], b"VIDEO", 0, tmp_path)
    _mse_chunk(manager, audio["session"], b"AUDIO", 0, tmp_path)

    reply = handle_message(
        {"action": "mse_end_all"}, tmp_path, mse_sessions=manager
    )
    assert reply["ok"] is True, reply
    assert reply["merge_error"] is None
    merged = reply["merged"]
    assert merged and merged["ok"] is True
    assert Path(merged["path"]).read_bytes() == b"MERGED"
    assert merged["sources"] == [reply["files"][0]["path"], reply["files"][1]["path"]]
    command = commands[0]
    assert command[0] == "ffmpeg-fake"
    assert "0:v:0" in command and "1:a:0" in command
    assert "-c" in command and "copy" in command


def test_mse_per_track_and_total_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_host, "MAX_MSE_TRACK", 8)
    monkeypatch.setattr(browser_host, "MAX_MSE_TOTAL", 12)
    manager = MseSessions(tmp_path)
    video = handle_message(
        {
            "action": "mse_start",
            "mime": "video/mp4",
            "filename": "clip.mp4",
            "kind": "video",
            "track": "v",
        },
        tmp_path,
        mse_sessions=manager,
    )
    audio = handle_message(
        {
            "action": "mse_start",
            "mime": "audio/mp4",
            "filename": "clip.mp4",
            "kind": "audio",
            "track": "a",
        },
        tmp_path,
        mse_sessions=manager,
    )
    assert _mse_chunk(manager, video["session"], b"12345678", 0, tmp_path)["ok"] is True
    per_track = _mse_chunk(manager, video["session"], b"9", 1, tmp_path)
    assert per_track["ok"] is False and "track exceeds" in per_track["error"]

    per_total = _mse_chunk(manager, audio["session"], b"12345", 0, tmp_path)
    assert per_total["ok"] is False and "capture exceeds" in per_total["error"]


def test_mse_low_disk_space_stops(tmp_path, monkeypatch):
    manager = MseSessions(tmp_path)
    monkeypatch.setattr(browser_host, "MIN_FREE_MSE_BYTES", 1)
    monkeypatch.setattr(browser_host, "_mse_free_bytes", lambda root: 2)
    start = handle_message(
        {
            "action": "mse_start",
            "mime": "video/mp4",
            "filename": "clip.mp4",
            "kind": "video",
            "track": "v",
        },
        tmp_path,
        mse_sessions=manager,
    )
    assert start["ok"] is True, start
    reply = _mse_chunk(manager, start["session"], b"12", 0, tmp_path)
    assert reply["ok"] is False and "low disk space" in reply["error"]
    # The rejected chunk must not be written or counted.
    assert manager.sessions[start["session"]]["total"] == 0


def test_mse_limit_env_parser(monkeypatch):
    monkeypatch.setenv("PDM_MSE_MAX_TRACK_BYTES", "12345")
    assert browser_host._env_bytes("PDM_MSE_MAX_TRACK_BYTES", 1) == 12345
    monkeypatch.setenv("PDM_MSE_MAX_TRACK_BYTES", "0")
    assert browser_host._env_bytes("PDM_MSE_MAX_TRACK_BYTES", 7) == 7
    monkeypatch.setenv("PDM_MSE_MAX_TRACK_BYTES", "not-a-number")
    assert browser_host._env_bytes("PDM_MSE_MAX_TRACK_BYTES", 7) == 7
    monkeypatch.delenv("PDM_MSE_MAX_TRACK_BYTES")
    assert browser_host._env_bytes("PDM_MSE_MAX_TRACK_BYTES", 7) == 7


def test_mse_webm_and_unknown_track_naming(tmp_path):
    manager, session = _mse_start(
        tmp_path,
        mime="video/webm; codecs=vp9",
        filename="clip.mp4",
        kind="video",
        track="v",
    )
    assert manager.sessions[session]["filename"].endswith(".webm")

    manager, session = _mse_start(
        tmp_path,
        mime="audio/webm; codecs=opus",
        filename="clip",
        kind="audio",
        track="a",
    )
    assert manager.sessions[session]["filename"].endswith(".webm")

    manager, session = _mse_start(
        tmp_path,
        mime="audio/mp4",
        filename="clip.mp4",
        kind="audio",
        track="a2",
    )
    assert manager.sessions[session]["filename"].endswith(".m4a")

    manager, session = _mse_start(
        tmp_path,
        mime="video/mp4",
        filename="raw",
        kind="unknown",
        track="u",
    )
    assert manager.sessions[session]["filename"].endswith(".mp4")


def test_mse_abort_all_removes_parts(tmp_path):
    manager = MseSessions(tmp_path)
    video = handle_message(
        {
            "action": "mse_start",
            "mime": "video/mp4",
            "filename": "clip.mp4",
            "kind": "video",
            "track": "v",
        },
        tmp_path,
        mse_sessions=manager,
    )
    audio = handle_message(
        {
            "action": "mse_start",
            "mime": "audio/mp4",
            "filename": "clip.mp4",
            "kind": "audio",
            "track": "a",
        },
        tmp_path,
        mse_sessions=manager,
    )
    _mse_chunk(manager, video["session"], b"VIDEO", 0, tmp_path)
    _mse_chunk(manager, audio["session"], b"AUDIO", 0, tmp_path)
    parts = [manager.sessions[video["session"]]["part"], manager.sessions[audio["session"]]["part"]]
    reply = handle_message(
        {"action": "mse_abort_all"}, tmp_path, mse_sessions=manager
    )
    assert reply["ok"] is True and reply["aborted"] == 2
    assert all(not path.exists() for path in parts)


def test_mse_end_all_without_data_fails_cleanly(tmp_path):
    manager = MseSessions(tmp_path)
    reply = handle_message(
        {"action": "mse_end_all"}, tmp_path, mse_sessions=manager
    )
    assert reply["ok"] is False
    assert "no MSE data" in reply["error"]


def test_mse_end_all_writes_one_gui_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_host, "_configured_ffmpeg", lambda: None)
    manager = MseSessions(tmp_path)
    video = handle_message(
        {
            "action": "mse_start",
            "mime": "video/mp4",
            "filename": "clip.mp4",
            "kind": "video",
            "track": "v",
            "source_url": "https://example.com/watch?v=1",
            "title": "Demo video",
        },
        tmp_path,
        mse_sessions=manager,
    )
    audio = handle_message(
        {
            "action": "mse_start",
            "mime": "audio/mp4",
            "filename": "clip.mp4",
            "kind": "audio",
            "track": "a",
            "source_url": "https://example.com/watch?v=1",
            "title": "Demo video",
        },
        tmp_path,
        mse_sessions=manager,
    )
    assert _mse_chunk(manager, video["session"], b"VIDEO", 0, tmp_path)["ok"] is True
    assert _mse_chunk(manager, audio["session"], b"AUDIO", 0, tmp_path)["ok"] is True

    reply = handle_message(
        {"action": "mse_end_all"}, tmp_path, mse_sessions=manager
    )
    assert reply["ok"] is True, reply
    assert reply["manifest_error"] is None
    manifests = list((tmp_path / "mse").glob("mse-session-*.json"))
    assert len(manifests) == 1
    assert reply["manifest"] == str(manifests[0])

    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["kind"] == "mse"
    assert manifest["source_url"] == "https://example.com/watch?v=1"
    assert manifest["title"] == "Demo video"
    assert manifest["track_count"] == 2
    assert {item["track"] for item in manifest["files"]} == {"v", "a"}
    assert manifest["merged"] is None
    assert "ffmpeg not found" in manifest["merge_error"]
    for item in manifest["files"]:
        path = Path(item["path"])
        assert path.is_file()
        assert path.parent == tmp_path / "mse"
        assert item["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_mse_single_end_writes_gui_manifest(tmp_path):
    manager, session = _mse_start(tmp_path, filename="single.mp4")
    assert _mse_chunk(manager, session, b"ONLY", 0, tmp_path)["ok"] is True
    reply = handle_message(
        {"action": "mse_end", "session": session},
        tmp_path,
        mse_sessions=manager,
    )
    assert reply["ok"] is True, reply
    manifests = list((tmp_path / "mse").glob("mse-session-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["files"][0]["path"] == reply["path"]
    assert manifest["files"][0]["size"] == len(b"ONLY")


def test_mse_extension_sends_page_metadata():
    background = (
        ROOT / "browser" / "extension" / "background.js"
    ).read_text(encoding="utf-8")
    assert "source_url: mseState.sourceUrl" in background
    assert "title: mseState.title" in background
    host_source = (
        ROOT / "src" / "personal_download_manager" / "browser_host.py"
    ).read_text(encoding="utf-8")
    assert 'message.get("source_url")' in host_source
    assert 'message.get("title")' in host_source


def test_mse_ffmpeg_timeout_is_reported(monkeypatch):
    def boom(*_args, **_kwargs):
        raise browser_host.subprocess.TimeoutExpired("ffmpeg", 120)

    monkeypatch.setattr(browser_host.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="timed out after 120s"):
        browser_host._run_ffmpeg(["ffmpeg"])


def test_mse_webm_page_and_fixtures():
    browser = ROOT / "browser"
    page = (browser / "mse-webm-test.html").read_text(encoding="utf-8")
    assert "playWebm" in page
    assert 'video/webm; codecs="vp9"' in page
    assert 'audio/webm; codecs="opus"' in page
    assert "/media/mse-webm-video.webm" in page
    assert "/media/mse-webm-audio.webm" in page

    for name, codec in (
        ("mse-webm-video.webm", b"V_VP9"),
        ("mse-webm-audio.webm", b"A_OPUS"),
    ):
        data = (browser / name).read_bytes()
        assert data.startswith(b"\x1aE\xdf\xa3"), "fixture must be EBML/WebM"
        assert codec in data, f"{name} must contain {codec.decode()}"

    script = (ROOT / "bench" / "mse_webm_verify.mjs").read_text(encoding="utf-8")
    assert "mse-webm-test.html" in script
    assert "playWebm" in script
    assert "mseStop" in script

    matrix = (ROOT / "bench" / "mse_site_matrix.mjs").read_text(encoding="utf-8")
    for case_id in (
        "dashif-akamai-bbb",
        "dashif-shaka-angel-one",
        "dashif-unified-tears",
        "hlsjs-shaka-angel-fmp4",
        "local-webm",
    ):
        assert case_id in matrix
    assert "PDM_MATRIX_ONLY" in matrix

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "serve_demo_webm", ROOT / "browser" / "serve_demo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ROUTES["/media/mse-webm-video.webm"][1] == "video/webm"
    assert module.ROUTES["/media/mse-webm-audio.webm"][1] == "audio/webm"
