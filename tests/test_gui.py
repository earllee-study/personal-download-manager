from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from personal_download_manager.gui.dialogs import (
    AddDownloadDialog,
    BatchAddDialog,
    CategoryDialog,
    HostPolicyDialog,
    ProxyGroupDialog,
    SettingsDialog,
)
from personal_download_manager.gui.manager import QueueManager
from personal_download_manager.gui.model import (
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PAUSED,
    DownloadTableModel,
    DownloadTask,
    TaskFilterProxy,
)
from personal_download_manager.gui.settings import AppSettings, governor_from_settings
from personal_download_manager.gui.update_dialog import UpdateDialog
from personal_download_manager.gui.worker import DownloadWorker
from personal_download_manager.update import UpdateInfo
from server import Route, make_content


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def settle(delay: float = 0.05) -> None:
    # Drain queued signals while the sender/receiver objects are still alive.
    # Without this, a progress/finished event posted by a worker just before
    # the test ended can be delivered during the *next* test's event loop,
    # where PySide6 occasionally crashes if the receiver was already collected.
    QApplication.processEvents()
    time.sleep(delay)
    QApplication.processEvents()


def wait_until(predicate, timeout: float = 20.0) -> bool:
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(25)
    state = {"done": False}

    def tick() -> None:
        if predicate():
            state["done"] = True
            timer.stop()
            loop.quit()

    timer.timeout.connect(tick)
    timer.start()
    QTimer.singleShot(int(timeout * 1000), loop.quit)
    loop.exec()
    timer.stop()
    return state["done"]


def test_settings_clamps_and_roundtrips(tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path / "downloads"),
        default_connections=99,
        max_concurrent=0,
        speed_limit=-1,
        theme="not-a-theme",
        update_manifest="  https://example.com/latest.json  ",
        ffmpeg_path="  D:\\tools\\ffmpeg.exe  ",
    )
    assert settings.update_manifest == "https://example.com/latest.json"
    assert settings.ffmpeg_path == "D:\\tools\\ffmpeg.exe"
    assert settings.default_connections == 64
    assert settings.max_concurrent == 1
    assert settings.speed_limit is None
    assert settings.theme == "system"
    assert (tmp_path / "downloads").exists()

    path = tmp_path / "config.json"
    settings.save(path)
    loaded = AppSettings.load(path)
    assert loaded.default_connections == 64
    assert loaded.download_dir == settings.download_dir
    assert loaded.update_manifest == "https://example.com/latest.json"
    assert loaded.ffmpeg_path == "D:\\tools\\ffmpeg.exe"


def test_settings_dialog_saves_update_manifest(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    dialog = SettingsDialog(settings)
    dialog.update_manifest_edit.setText(" https://example.com/latest.json ")
    dialog.accept()
    assert settings.update_manifest == "https://example.com/latest.json"
    dialog.deleteLater()
    QApplication.processEvents()


def test_settings_dialog_saves_ffmpeg_path(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    dialog = SettingsDialog(settings)
    dialog.ffmpeg_edit.setText("  D:\\tools\\ffmpeg\\bin\\ffmpeg.exe  ")
    dialog.accept()
    assert settings.ffmpeg_path == "D:\\tools\\ffmpeg\\bin\\ffmpeg.exe"
    dialog.deleteLater()
    QApplication.processEvents()


def test_worker_passes_ffmpeg_setting(qapp, tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path), ffmpeg_path="D:\\tools\\ffmpeg.exe"
    )
    task = DownloadTask(
        url="https://example.com/vod/manifest.mpd",
        output=str(tmp_path / "movie.mp4"),
        options={"dash": True, "dash_track": "both"},
    )
    config = DownloadWorker(task, settings)._build_config()
    assert config.ffmpeg_path == "D:\\tools\\ffmpeg.exe"


def test_update_dialog_states_without_network(qapp):
    dialog = UpdateDialog("https://example.com/latest.json", "0.1.0", start=False)
    try:
        assert "0.1.0" in dialog.status.text()
        dialog._on_checked(None)
        assert "最新" in dialog.status.text()
        assert not dialog.download_button.isEnabled()

        info = UpdateInfo(
            version="0.2.0",
            url="https://example.com/setup.exe",
            sha256="0" * 64,
            size=1024 * 1024,
            notes="test notes",
            published_at="2026-11-01",
        )
        dialog._on_checked(info)
        assert "0.2.0" in dialog.status.text()
        assert dialog.download_button.isEnabled()
        assert not dialog.notes.isHidden()
        assert "test notes" in dialog.notes.toPlainText()
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_update_dialog_checks_in_background(qapp):
    dialog = UpdateDialog(
        "https://example.com/latest.json",
        "0.1.0",
        start=True,
        check_func=lambda: None,
    )
    try:
        assert wait_until(lambda: "最新" in dialog.status.text(), 5)
        assert dialog.close_button.isEnabled()
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_update_dialog_downloads_and_asks_before_launch(
    qapp, tmp_path, monkeypatch
):
    info = UpdateInfo(
        version="0.2.0",
        url="https://example.com/setup.exe",
        sha256="0" * 64,
        size=4,
    )

    def fake_download(info, directory, **kwargs):
        progress = kwargs.get("progress")
        if progress:
            progress(4, 4)
        path = Path(directory) / "setup.exe"
        path.write_bytes(b"MZxx")
        return path

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )
    launched = []
    dialog = UpdateDialog(
        "https://example.com/latest.json",
        "0.1.0",
        start=False,
        download_func=fake_download,
        launch_func=lambda path: launched.append(path),
        download_dir=tmp_path,
    )
    try:
        dialog._on_checked(info)
        dialog._download()
        assert wait_until(
            lambda: dialog._downloaded is not None and dialog._busy is None, 5
        )
        assert (tmp_path / "setup.exe").exists()
        assert not launched  # answer was "No"
        assert "校验" in dialog.status.text()
    finally:
        dialog.deleteLater()
        QApplication.processEvents()


def test_settings_host_policy_roundtrip(tmp_path):
    mib = 1024 * 1024
    settings = AppSettings(
        download_dir=str(tmp_path),
        global_speed_limit=5 * mib,
        max_host_connections=99,
        host_rules=[{"pattern": "*.test", "connections": 2, "proxy": "http://p:1"}],
    )
    assert settings.max_host_connections == 64
    path = tmp_path / "config.json"
    settings.save(path)
    loaded = AppSettings.load(path)
    assert loaded.global_speed_limit == 5 * mib
    assert loaded.max_host_connections == 64
    assert loaded.host_rules == settings.host_rules

    governor = governor_from_settings(loaded)
    assert governor.global_speed_limit == 5 * mib
    assert governor.resolve("https://a.test/file").connections == 2
    assert governor.resolve("https://a.test/file").proxy == "http://p:1"
    assert governor.resolve("https://other.org/file").connections == 64


def test_host_policy_dialog_parses_rows(qapp):
    dialog = HostPolicyDialog(
        [{"pattern": "*.test", "connections": 2, "speed_limit": 1024, "proxy": None}]
    )
    assert dialog.table.rowCount() == 1
    dialog._add_row({"pattern": "example.com", "speed_limit": "1M", "proxy": "http://p"})
    dialog.accept()
    assert dialog.rules == [
        {
            "pattern": "*.test",
            "connections": 2,
            "speed_limit": 1024,
            "proxy": None,
            "proxy_group": None,
            "user_agent": None,
            "cookie": None,
            "auth": None,
            "auth_type": "basic",
        },
        {
            "pattern": "example.com",
            "connections": None,
            "speed_limit": 1024 * 1024,
            "proxy": "http://p",
            "proxy_group": None,
            "user_agent": None,
            "cookie": None,
            "auth": None,
            "auth_type": "basic",
        },
    ]


def test_host_policy_dialog_parses_site_credentials(qapp):
    dialog = HostPolicyDialog([])
    dialog._add_row(
        {
            "pattern": "*.example.com",
            "user_agent": "SiteBot/1.0",
            "cookie": "sid=42",
            "auth": "bob:pw",
            "auth_type": "digest",
        }
    )
    dialog.accept()
    assert dialog.rules[0]["user_agent"] == "SiteBot/1.0"
    assert dialog.rules[0]["cookie"] == "sid=42"
    assert dialog.rules[0]["auth"] == "bob:pw"
    assert dialog.rules[0]["auth_type"] == "digest"


def test_settings_proxy_groups_roundtrip_and_resolution(tmp_path):
    settings = AppSettings(
        download_dir=str(tmp_path),
        proxy_groups=[
            {"name": "us", "proxy": "socks5://u:1080"},
            {"name": " us ", "proxy": "http://ignored:1"},  # duplicate
            {"name": "", "proxy": "http://blank"},          # invalid
        ],
        host_rules=[{"pattern": "*.example.com", "proxy_group": "us"}],
    )
    assert settings.proxy_groups == [{"name": "us", "proxy": "socks5://u:1080"}]

    path = tmp_path / "config.json"
    settings.save(path)
    loaded = AppSettings.load(path)
    assert loaded.proxy_groups == settings.proxy_groups
    governor = governor_from_settings(loaded)
    assert governor.resolve("https://a.example.com/x").proxy == "socks5://u:1080"
    assert governor.resolve("https://other.test/x").proxy is None


def test_proxy_group_dialog_parses_rows(qapp):
    dialog = ProxyGroupDialog([{"name": "us", "proxy": "socks5://u:1080"}])
    dialog._add_row({"name": "eu", "proxy": "http://e:8080"})
    dialog.accept()
    assert dialog.groups == [
        {"name": "us", "proxy": "socks5://u:1080"},
        {"name": "eu", "proxy": "http://e:8080"},
    ]


def test_host_policy_dialog_resolves_proxy_group(qapp):
    dialog = HostPolicyDialog(
        [], [{"name": "us", "proxy": "socks5://u:1080"}]
    )
    dialog._add_row({"pattern": "*.example.com", "proxy_group": "us"})
    dialog.accept()
    assert dialog.rules[0]["proxy_group"] == "us"


def test_host_policy_dialog_rejects_unknown_proxy_group(qapp, monkeypatch):
    import personal_download_manager.gui.dialogs as dialogs

    warnings: list = []
    monkeypatch.setattr(
        dialogs.QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args)
    )
    dialog = HostPolicyDialog([], [])
    dialog._add_row({"pattern": "x.test", "proxy_group": "ghost"})
    dialog.accept()
    assert warnings
    assert dialog.rules == []


def test_unknown_size_uses_fraction_progress(qapp):
    from personal_download_manager.gui.model import COL_SIZE

    task = DownloadTask(
        url="https://cdn/v.m3u8",
        output="v.ts",
        status=STATUS_DOWNLOADING,
        total_size=None,
        downloaded=1234,
        progress_fraction=0.42,
    )
    assert task.progress == 42.0
    assert DownloadTableModel._display(task, COL_SIZE).endswith("/ ?")

    task.progress_fraction = None
    task.total_size = 2000
    assert task.progress == 61.7


def test_model_filters_and_interrupted_status(qapp):
    model = DownloadTableModel()
    proxy = TaskFilterProxy()
    proxy.setSourceModel(model)
    model.add_task(
        DownloadTask(url="https://x/alpha.bin", output="alpha.bin", status=STATUS_COMPLETED)
    )
    model.add_task(
        DownloadTask(url="https://x/beta.bin", output="beta.bin", status=STATUS_FAILED)
    )
    model.add_task(
        DownloadTask(url="https://x/gamma.bin", output="gamma.bin", status=STATUS_PAUSED)
    )
    assert proxy.rowCount() == 3
    proxy.set_status_filter("completed")
    assert proxy.rowCount() == 1
    proxy.set_status_filter("all")
    proxy.set_search("beta")
    assert proxy.rowCount() == 1
    proxy.set_search("")

    task = DownloadTask.from_dict(
        {"url": "https://x/a", "output": "a", "status": "downloading"}
    )
    assert task.status == STATUS_PAUSED


def test_queue_download_end_to_end(qapp, server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=70)
    server.add("/gui.bin", Route(content=data, etag='"gui"'))
    settings = AppSettings(
        download_dir=str(tmp_path), max_concurrent=2, default_connections=4
    )
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    task = DownloadTask(
        url=server.url("/gui.bin"),
        output=str(tmp_path / "gui.bin"),
        options={"connections": 4, "min_segment": "512K", "min_split": "512K"},
    )
    manager.add_task(task, start=True)
    assert wait_until(
        lambda: (model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    settle()
    finished = model.find(task.id)
    assert finished is not None
    assert finished.status == STATUS_COMPLETED, finished.error
    assert (tmp_path / "gui.bin").read_bytes() == data


def test_add_dialog_detects_hls_playlists(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    dialog = AddDownloadDialog(
        settings, None, "https://example.com/vod/master.m3u8"
    )
    assert dialog.hls_check.isChecked()
    assert dialog.name_edit.text().endswith(".ts")

    dialog.accept()
    assert dialog.result_task is not None
    assert dialog.result_task.options["hls"] is True
    assert dialog.result_task.options["hls_quality"] == "best"
    assert dialog.result_task.output.endswith(".ts")


def test_queue_hls_download_end_to_end(qapp, server, tmp_path):
    seg0 = make_content(256 * 1024, seed=200)
    seg1 = make_content(256 * 1024, seed=201)
    server.add("/gm/seg0.ts", Route(content=seg0))
    server.add("/gm/seg1.ts", Route(content=seg1))
    playlist = server.add("/gm/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n#EXTINF:1,\nseg0.ts\n#EXTINF:1,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()

    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    task = DownloadTask(
        url=server.url("/gm/media.m3u8"),
        output=str(tmp_path / "gm.ts"),
        options={"hls": True, "hls_quality": "best", "connections": 2},
    )
    manager.add_task(task, start=True)

    assert wait_until(
        lambda: (model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    settle()
    finished = model.find(task.id)
    assert finished is not None
    assert finished.status == STATUS_COMPLETED, finished.error
    assert (tmp_path / "gm.ts").read_bytes() == seg0 + seg1


def test_browser_capture_becomes_a_task(qapp, server, tmp_path):
    import json

    from personal_download_manager.gui.capture import load_captures, make_task

    data = make_content(256 * 1024, seed=210)
    server.add("/cap.bin", Route(content=data, etag='"cap"'))
    spool = tmp_path / "capture"
    spool.mkdir()
    (spool / "capture-1.json").write_text(
        json.dumps(
            {
                "url": server.url("/cap.bin"),
                "filename": "browser.bin",
                "referer": "https://example.com/",
            }
        ),
        encoding="utf-8",
    )

    captures = load_captures(spool)
    assert len(captures) == 1
    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    task = make_task(captures[0][1], tmp_path, settings)
    assert task.options["referer"] == "https://example.com/"

    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    manager.add_task(task)
    assert wait_until(
        lambda: (model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    settle()
    finished = model.find(task.id)
    assert finished.status == STATUS_COMPLETED, finished.error
    assert (tmp_path / "browser.bin").read_bytes() == data


def test_hls_capture_uses_the_hls_engine(qapp, server, tmp_path):
    from personal_download_manager.gui.capture import Capture, make_task

    seg0 = make_content(128 * 1024, seed=220)
    seg1 = make_content(128 * 1024, seed=221)
    server.add("/hc/seg0.ts", Route(content=seg0))
    server.add("/hc/seg1.ts", Route(content=seg1))
    playlist = server.add("/hc/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n#EXTINF:1,\nseg0.ts\n#EXTINF:1,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()

    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    capture = Capture(url=server.url("/hc/media.m3u8"), filename="media.m3u8", hls=True)
    task = make_task(capture, tmp_path, settings)
    assert task.options["hls"] is True
    assert task.output.endswith(".ts")

    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    manager.add_task(task)
    assert wait_until(
        lambda: (model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    settle()
    finished = model.find(task.id)
    assert finished.status == STATUS_COMPLETED, finished.error
    # .ts is a video extension, so the default auto-category moves it there.
    assert (tmp_path / "视频" / "media.ts").read_bytes() == seg0 + seg1


def test_main_window_queues_browser_captures(qapp, server, tmp_path, monkeypatch):
    import json

    import personal_download_manager.gui.main_window as main_window

    data = make_content(128 * 1024, seed=211)
    server.add("/mw.bin", Route(content=data, etag='"mw"'))
    spool = tmp_path / "capture"
    spool.mkdir()
    monkeypatch.setattr(main_window, "capture_dir", lambda: spool)
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)

    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    settings.save = lambda path=None: None  # never touch the real %APPDATA%
    window = main_window.MainWindow(settings)
    try:
        (spool / "capture-xyz.json").write_text(
            json.dumps({"url": server.url("/mw.bin"), "filename": "mw.bin"}),
            encoding="utf-8",
        )
        window._poll_captures()
        tasks = window.model.tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.url == server.url("/mw.bin")
        assert not list(spool.glob("capture-*.json"))

        assert wait_until(
            lambda: (window.model.find(task.id) or task).status
            in (STATUS_COMPLETED, STATUS_FAILED),
            20,
        )
        assert (window.model.find(task.id) or task).status == STATUS_COMPLETED
        assert (tmp_path / "mw.bin").read_bytes() == data
    finally:
        window.manager.shutdown()
        settle()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window.hide()
        window.deleteLater()
        QApplication.processEvents()


def test_queue_pause_then_resume(qapp, server, tmp_path):
    data = make_content(8 * 1024 * 1024, seed=71)
    route = server.add(
        "/pause.bin",
        Route(content=data, etag='"pause"', throttle_bps=1024 * 1024),
    )
    settings = AppSettings(
        download_dir=str(tmp_path), max_concurrent=2, default_connections=4
    )
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    task = DownloadTask(
        url=server.url("/pause.bin"),
        output=str(tmp_path / "pause.bin"),
        options={"connections": 4, "min_segment": "512K", "min_split": "512K"},
    )
    manager.add_task(task, start=True)

    assert wait_until(lambda: (model.find(task.id) or task).downloaded > 200_000, 10)
    manager.pause(task.id)
    assert wait_until(
        lambda: (model.find(task.id) or task).status == STATUS_PAUSED, 10
    )
    assert (tmp_path / "pause.bin.pdm.part").exists()

    route.throttle_bps = None
    manager.resume(task.id)
    assert wait_until(
        lambda: (model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    settle()
    finished = model.find(task.id)
    assert finished is not None
    assert finished.status == STATUS_COMPLETED, finished.error
    assert (tmp_path / "pause.bin").read_bytes() == data


def test_settings_categories_roundtrip(tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    assert any(item["name"] == "视频" for item in settings.categories)
    assert settings.default_category == "auto"

    path = tmp_path / "config.json"
    settings.save(path)
    loaded = AppSettings.load(path)
    assert loaded.categories == settings.categories
    assert loaded.categories_enabled is True
    assert loaded.default_category == "auto"

    custom = AppSettings(
        download_dir=str(tmp_path),
        categories=[{"name": "书", "folder": "Books", "extensions": ["epub"]}],
        default_category="auto",
    )
    assert custom.category_folder("a.epub") == "Books"
    assert custom.category_folder("a.mp4") is None
    assert custom.category_folder("a.epub", "none") is None
    explicit = AppSettings(
        download_dir=str(tmp_path), categories=custom.categories, default_category="书"
    )
    assert explicit.category_folder("a.mp4") == "Books"


def test_add_dialog_applies_auto_category(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    dialog = AddDownloadDialog(settings, None, "https://example.com/clip.mp4")
    assert dialog.category_combo.currentData() == "auto"
    dialog.accept()
    assert dialog.result_task is not None
    assert Path(dialog.result_task.output) == tmp_path / "视频" / "clip.mp4"
    assert dialog.result_task.options["category"] == "auto"

    plain = AddDownloadDialog(settings, None, "https://example.com/clip.mp4")
    plain.category_combo.setCurrentIndex(plain.category_combo.findData("none"))
    plain.accept()
    assert plain.result_task is not None
    assert Path(plain.result_task.output) == tmp_path / "clip.mp4"


def test_add_dialog_respects_disabled_categories(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)
    dialog = AddDownloadDialog(settings, None, "https://example.com/song.mp3")
    assert not dialog.category_combo.isEnabled()
    dialog.accept()
    assert dialog.result_task is not None
    assert Path(dialog.result_task.output) == tmp_path / "song.mp3"


def test_add_dialog_accepts_ftp_and_sftp_urls(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)

    ftp = AddDownloadDialog(
        settings, None, "ftp://user:pw@example.com/dir/archive.zip"
    )
    ftp.ftp_active_check.setChecked(True)
    ftp.accept()
    assert ftp.result_task is not None
    assert ftp.result_task.url.startswith("ftp://")
    assert Path(ftp.result_task.output).name == "archive.zip"
    assert ftp.result_task.options["ftp_active"] is True

    sftp = AddDownloadDialog(
        settings, None, "sftp://user@example.com/files/doc.pdf"
    )
    sftp.sftp_key_edit.setText("C:/keys/id_ed25519")
    sftp.sftp_key_pass_edit.setText("secret")
    sftp.sftp_auto_host_check.setChecked(True)
    sftp.accept()
    assert sftp.result_task is not None
    options = sftp.result_task.options
    assert options["sftp_key"] == "C:/keys/id_ed25519"
    assert options["sftp_key_pass"] == "secret"
    assert options["sftp_auto_host_key"] is True


def test_ftp_playlist_named_file_is_not_streamed(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)
    dialog = AddDownloadDialog(settings, None, "ftp://host/live.m3u8")
    assert dialog.hls_check.isChecked() is False
    dialog.accept()
    assert dialog.result_task is not None
    assert dialog.result_task.options["hls"] is False
    assert dialog.result_task.output.endswith(".m3u8")


def test_batch_dialog_accepts_ftp_patterns(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)
    dialog = BatchAddDialog(settings)
    dialog.patterns_edit.setPlainText(
        "ftp://mirror.example/pub/a[1-2].bin\nsftp://user@host/b.bin"
    )
    dialog.accept()
    assert dialog.urls == [
        "ftp://mirror.example/pub/a1.bin",
        "ftp://mirror.example/pub/a2.bin",
        "sftp://user@host/b.bin",
    ]


def test_worker_does_not_apply_http_proxy_to_ftp(qapp, tmp_path):
    from personal_download_manager.gui.worker import DownloadWorker

    settings = AppSettings(download_dir=str(tmp_path), proxy="http://proxy:8080")
    worker = DownloadWorker(
        DownloadTask(url="ftp://host/file.bin", output=str(tmp_path / "f.bin")),
        settings,
    )
    try:
        assert worker._build_config().proxy is None
    finally:
        worker.deleteLater()
        QApplication.processEvents()

    active = DownloadWorker(
        DownloadTask(
            url="ftp://host/file.bin",
            output=str(tmp_path / "a.bin"),
            options={"ftp_active": True},
        ),
        settings,
    )
    try:
        assert active._build_config().ftp_active is True
    finally:
        active.deleteLater()
        QApplication.processEvents()

    http = DownloadWorker(
        DownloadTask(url="https://host/file.bin", output=str(tmp_path / "h.bin")),
        settings,
    )
    try:
        assert http._build_config().proxy == "http://proxy:8080"
    finally:
        http.deleteLater()
        QApplication.processEvents()


def test_add_dialog_http3_is_mutually_exclusive_with_http2(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path), categories_enabled=False)
    dialog = AddDownloadDialog(settings, None, "https://example.com/file.bin")
    dialog.http2_check.setChecked(True)
    dialog.http3_check.setChecked(True)
    assert dialog.http2_check.isChecked() is False
    dialog.accept()
    assert dialog.result_task is not None
    assert dialog.result_task.options["http3"] is True
    assert dialog.result_task.options["http2"] is False


def test_batch_dialog_expands_patterns(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    dialog = BatchAddDialog(settings)
    dialog.patterns_edit.setPlainText(
        "https://h.example/a[1-3].bin\nhttps://h.example/b.bin"
    )
    dialog.accept()
    assert dialog.urls == [
        "https://h.example/a1.bin",
        "https://h.example/a2.bin",
        "https://h.example/a3.bin",
        "https://h.example/b.bin",
    ]
    assert dialog.directory == str(tmp_path)
    assert dialog.category == "auto"
    assert dialog.start_now is True


def test_category_dialog_parses_rows(qapp):
    dialog = CategoryDialog(
        [{"name": "书", "folder": "Books", "extensions": ["epub", "mobi"]}]
    )
    dialog._add_row({"name": "壁纸", "folder": "Wallpapers", "extensions": "jpg, png"})
    dialog.accept()
    assert dialog.categories == [
        {"name": "书", "folder": "Books", "extensions": ["epub", "mobi"]},
        {"name": "壁纸", "folder": "Wallpapers", "extensions": ["jpg", "png"]},
    ]


def test_main_window_quick_add_uses_category(qapp, tmp_path, monkeypatch):
    import personal_download_manager.gui.main_window as main_window

    monkeypatch.setattr(main_window, "capture_dir", lambda: tmp_path / "capture")
    monkeypatch.setattr(main_window, "load_downloads", lambda *a, **k: [])
    monkeypatch.setattr(main_window, "save_downloads", lambda *a, **k: None)

    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=1)
    settings.save = lambda path=None: None
    window = main_window.MainWindow(settings)
    try:
        task = window._add_quick("https://example.com/song.mp3", start=False)
        assert Path(task.output) == tmp_path / "音乐" / "song.mp3"
        assert task.options["category"] == "auto"

        other = window._add_quick(
            "https://example.com/tool.exe", category="none", start=False
        )
        assert Path(other.output) == tmp_path / "tool.exe"

        playlist = window._add_quick(
            "https://example.com/vod/master.m3u8", start=False
        )
        assert playlist.output.endswith(".ts")
        assert playlist.options["hls"] is True
        assert Path(playlist.output) == tmp_path / "视频" / "master.ts"

        remote_playlist = window._add_quick(
            "ftp://example.com/vod/live.m3u8", start=False
        )
        assert remote_playlist.output.endswith(".m3u8")
        assert remote_playlist.options["hls"] is False
        assert Path(remote_playlist.output) == tmp_path / "live.m3u8"
    finally:
        window.manager.shutdown()
        settle()
        window._capture_timer.stop()
        window._clip_timer.stop()
        window._bandwidth_timer.stop()
        window.hide()
        window.deleteLater()
        QApplication.processEvents()


def test_add_dialog_detects_dash_playlists(qapp, tmp_path):
    settings = AppSettings(download_dir=str(tmp_path))
    dialog = AddDownloadDialog(
        settings, None, "https://example.com/vod/manifest.mpd"
    )
    assert dialog.hls_check.isChecked()
    assert not dialog.dash_track.isHidden()
    assert dialog.name_edit.text() == "manifest.mp4"

    dialog.accept()
    task = dialog.result_task
    assert task is not None
    assert task.options["dash"] is True
    assert task.options["dash_track"] == "video"
    assert Path(task.output) == tmp_path / "视频" / "manifest.mp4"

    audio = AddDownloadDialog(
        settings, None, "https://example.com/vod/manifest.mpd"
    )
    audio.dash_track.setCurrentIndex(audio.dash_track.findData("audio"))
    assert audio.name_edit.text() == "manifest.m4a"
    audio.accept()
    assert audio.result_task is not None
    assert audio.result_task.options["dash_track"] == "audio"
    # .m4a belongs to the music category.
    assert Path(audio.result_task.output) == tmp_path / "音乐" / "manifest.m4a"

    both = AddDownloadDialog(
        settings, None, "https://example.com/vod/manifest.mpd"
    )
    both.dash_track.setCurrentIndex(both.dash_track.findData("both"))
    assert both.name_edit.text() == "manifest.mp4"
    both.accept()
    assert both.result_task is not None
    assert both.result_task.options["dash_track"] == "both"


def test_queue_dash_download_end_to_end(qapp, server, tmp_path):
    init = make_content(16 * 1024, seed=610)
    seg1 = make_content(32 * 1024, seed=611)
    seg2 = make_content(32 * 1024, seed=612)
    server.add("/gd/init.mp4", Route(content=init))
    server.add("/gd/s1.m4s", Route(content=seg1))
    server.add("/gd/s2.m4s", Route(content=seg2))
    mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
        'mediaPresentationDuration="PT4S"><Period>'
        '<AdaptationSet contentType="video" mimeType="video/mp4">'
        '<SegmentTemplate timescale="1" duration="2" '
        'initialization="init.mp4" media="s$Number$.m4s"/>'
        '<Representation id="v" bandwidth="500000" width="640" height="360"/>'
        "</AdaptationSet></Period></MPD>"
    )
    server.add("/gd/manifest.mpd", Route(content=mpd.encode()))

    settings = AppSettings(download_dir=str(tmp_path), max_concurrent=2)
    model = DownloadTableModel()
    manager = QueueManager(model, settings)
    task = DownloadTask(
        url=server.url("/gd/manifest.mpd"),
        output=str(tmp_path / "gd.mp4"),
        options={"dash": True, "dash_track": "video", "connections": 2},
    )
    manager.add_task(task, start=True)
    assert wait_until(
        lambda: (model.find(task.id) or task).status
        in (STATUS_COMPLETED, STATUS_FAILED),
        20,
    )
    manager.shutdown()
    settle()
    finished = model.find(task.id)
    assert finished is not None
    assert finished.status == STATUS_COMPLETED, finished.error
    assert (tmp_path / "gd.mp4").read_bytes() == init + seg1 + seg2
