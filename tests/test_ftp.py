"""FTP / explicit FTPS: ranges-by-REST, resume, auth, probe and the CLI."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("pyftpdlib")

from ftpserver import FtpServer  # noqa: E402
from server import make_content  # noqa: E402

from personal_download_manager.downloader import Downloader  # noqa: E402
from personal_download_manager.errors import (  # noqa: E402
    AuthRequired,
    Cancelled,
    ChecksumMismatch,
    FatalError,
    ProbeError,
)
from personal_download_manager.ftp import probe_ftp  # noqa: E402
from personal_download_manager.models import DownloadConfig  # noqa: E402


@pytest.fixture
def ftp(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    server = FtpServer(remote).start()
    try:
        yield server, remote, local
    finally:
        server.stop()


def _config(server, url_path: str, output, **overrides) -> DownloadConfig:
    defaults = dict(
        url=server.url(url_path),
        output=output,
        show_progress=False,
        quiet=True,
        timeout=10.0,
        connect_timeout=5.0,
        read_timeout=10.0,
        max_retries=2,
    )
    defaults.update(overrides)
    return DownloadConfig(**defaults)


def test_ftp_download_is_byte_exact(ftp):
    server, remote, local = ftp
    data = make_content(512 * 1024, seed=501)
    (remote / "file.bin").write_bytes(data)
    output = local / "file.bin"

    result = Downloader(_config(server, "/file.bin", output)).run()

    assert output.read_bytes() == data
    assert result.total_size == len(data)
    assert result.downloaded == len(data)
    # pyftpdlib supports REST; even the small single-segment path reports it.
    assert result.range_supported is True
    assert result.segments_created == 1
    assert not (local / "file.bin.pdm.part").exists()
    assert not (local / "file.bin.pdm.json").exists()


def test_ftp_multi_connection_download(ftp):
    server, remote, local = ftp
    data = make_content(4 * 1024 * 1024, seed=508)
    (remote / "multi.bin").write_bytes(data)
    output = local / "multi.bin"

    result = Downloader(
        _config(
            server,
            "/multi.bin",
            output,
            max_connections=4,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
    ).run()

    assert output.read_bytes() == data
    assert result.range_supported is True
    assert result.segments_created > 1
    assert result.peak_connections > 1
    assert not (local / "multi.bin.pdm.part").exists()
    assert not (local / "multi.bin.pdm.json").exists()
    # Default remains passive: the client asks for a server-side data port.
    assert any(mode in ("PASV", "EPSV") for mode in server.modes)
    assert "PORT" not in server.modes and "EPRT" not in server.modes


def test_ftp_active_mode_multi_connection(ftp):
    server, remote, local = ftp
    data = make_content(2 * 1024 * 1024, seed=512)
    (remote / "active.bin").write_bytes(data)
    output = local / "active.bin"

    result = Downloader(
        _config(
            server,
            "/active.bin",
            output,
            ftp_active=True,
            max_connections=4,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
    ).run()

    assert output.read_bytes() == data
    assert result.segments_created > 1
    assert result.peak_connections > 1
    # The client listens and tells the server where to connect (PORT/EPRT).
    assert any(mode in ("PORT", "EPRT") for mode in server.modes)
    assert "PASV" not in server.modes and "EPSV" not in server.modes


def test_ftps_active_mode_multi_connection(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    data = make_content(2 * 1024 * 1024, seed=513)
    (remote / "tls-active.bin").write_bytes(data)
    server = FtpServer(remote, tls=True).start()
    try:
        output = tmp_path / "tls-active.bin"
        result = Downloader(
            _config(
                server,
                "/tls-active.bin",
                output,
                verify=False,
                ftp_active=True,
                max_connections=2,
                min_segment_size=256 * 1024,
                min_split_size=256 * 1024,
            )
        ).run()
        assert output.read_bytes() == data
        assert result.segments_created > 1
        assert any(mode in ("PORT", "EPRT") for mode in server.modes)
        assert "PASV" not in server.modes and "EPSV" not in server.modes
    finally:
        server.stop()


def test_ftp_multi_resumes_after_cancel(ftp):
    server, remote, local = ftp
    data = make_content(2 * 1024 * 1024, seed=509)
    (remote / "multi-resume.bin").write_bytes(data)
    output = local / "multi-resume.bin"
    holder: dict = {}

    def progress(payload):
        if payload.downloaded > 300_000 and "downloader" in holder:
            holder["downloader"].cancel()

    options = dict(
        max_connections=4,
        min_segment_size=256 * 1024,
        min_split_size=256 * 1024,
        speed_limit=1024 * 1024,
    )
    first = Downloader(
        _config(server, "/multi-resume.bin", output, **options),
        progress_callback=progress,
    )
    holder["downloader"] = first
    with pytest.raises(Cancelled):
        first.run()
    assert (local / "multi-resume.bin.pdm.part").exists()

    result = Downloader(
        _config(
            server,
            "/multi-resume.bin",
            output,
            max_connections=4,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
    ).run()
    assert result.resumed is True
    assert output.read_bytes() == data


def test_ftp_falls_back_when_rest_is_unsupported(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    data = make_content(2 * 1024 * 1024, seed=510)
    (remote / "norest.bin").write_bytes(data)
    server = FtpServer(remote, allow_rest=False).start()
    try:
        output = tmp_path / "norest.bin"
        result = Downloader(
            _config(
                server,
                "/norest.bin",
                output,
                max_connections=4,
                min_segment_size=256 * 1024,
            )
        ).run()
        assert output.read_bytes() == data
        assert result.segments_created == 1
        assert result.range_supported is False
    finally:
        server.stop()


def test_ftps_multi_connection_download(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    data = make_content(2 * 1024 * 1024, seed=511)
    (remote / "tls-multi.bin").write_bytes(data)
    server = FtpServer(remote, tls=True).start()
    try:
        output = tmp_path / "tls-multi.bin"
        result = Downloader(
            _config(
                server,
                "/tls-multi.bin",
                output,
                verify=False,
                max_connections=2,
                min_segment_size=256 * 1024,
                min_split_size=256 * 1024,
            )
        ).run()
        assert output.read_bytes() == data
        assert result.segments_created > 1
        assert result.range_supported is True
    finally:
        server.stop()


def test_ftp_probe_reports_size_and_mtime(ftp):
    server, remote, _local = ftp
    data = make_content(128 * 1024, seed=502)
    (remote / "probe.bin").write_bytes(data)

    result = probe_ftp(_config(server, "/probe.bin", None))

    assert result.status == 213
    assert result.total_size == len(data)
    assert result.ranges is True  # REST capability probe
    assert result.filename == "probe.bin"
    assert result.http_version == "FTP"
    assert result.last_modified is not None


def test_ftp_resumes_after_cancel(ftp):
    server, remote, local = ftp
    data = make_content(1024 * 1024, seed=503)
    (remote / "resume.bin").write_bytes(data)
    output = local / "resume.bin"
    holder: dict = {}

    def progress(payload):
        if payload.downloaded > 200_000 and "downloader" in holder:
            holder["downloader"].cancel()

    first = Downloader(
        _config(
            server,
            "/resume.bin",
            output,
            speed_limit=256 * 1024,
        ),
        progress_callback=progress,
    )
    holder["downloader"] = first
    with pytest.raises(Cancelled):
        first.run()
    part = local / "resume.bin.pdm.part"
    assert part.exists()
    assert part.stat().st_size > 0

    result = Downloader(_config(server, "/resume.bin", output)).run()
    assert result.resumed is True
    assert output.read_bytes() == data


def test_ftp_wrong_password_is_fatal(ftp):
    server, remote, _local = ftp
    (remote / "file.bin").write_bytes(b"x")
    config = replace(
        _config(server, "/file.bin", None),
        url=server.url("/file.bin", password="wrong"),
    )
    with pytest.raises(AuthRequired):
        Downloader(config).run()


def test_ftp_missing_file_fails(ftp):
    server, _remote, local = ftp
    output = local / "missing.bin"

    with pytest.raises((ProbeError, FatalError)) as excinfo:
        Downloader(_config(server, "/missing.bin", output)).run()
    assert "550" in str(excinfo.value) or "not found" in str(excinfo.value).lower()


def test_ftp_uses_auth_option_when_url_has_no_credentials(ftp):
    server, remote, local = ftp
    data = make_content(32 * 1024, seed=507)
    (remote / "auth.bin").write_bytes(data)
    output = local / "auth.bin"
    config = replace(
        _config(server, "/auth.bin", output),
        url=f"ftp://127.0.0.1:{server.port}/auth.bin",
        auth=("user", "pw"),
    )
    Downloader(config).run()
    assert output.read_bytes() == data


def test_ftp_sha256_is_verified(ftp):
    server, remote, local = ftp
    data = make_content(64 * 1024, seed=504)
    (remote / "sum.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    output = local / "sum.bin"

    result = Downloader(
        _config(server, "/sum.bin", output, sha256=digest)
    ).run()
    assert result.sha256 == digest
    assert output.read_bytes() == data

    output2 = local / "sum-bad.bin"
    with pytest.raises(ChecksumMismatch):
        Downloader(
            _config(server, "/sum.bin", output2, sha256="0" * 64)
        ).run()


def test_ftp_rejects_proxy(ftp):
    server, remote, local = ftp
    (remote / "file.bin").write_bytes(b"x")
    with pytest.raises(FatalError, match="proxy"):
        Downloader(
            _config(
                server,
                "/file.bin",
                local / "x.bin",
                proxy="socks5://127.0.0.1:1080",
            )
        ).run()


def test_ftps_explicit_tls_download(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    data = make_content(128 * 1024, seed=505)
    (remote / "tls.bin").write_bytes(data)
    server = FtpServer(remote, tls=True).start()
    try:
        output = tmp_path / "tls.bin"
        config = _config(server, "/tls.bin", output, verify=False)
        result = Downloader(config).run()
        assert output.read_bytes() == data
        assert probe_ftp(_config(server, "/tls.bin", None, verify=False)).http_version == "FTPS"
        assert result.total_size == len(data)
    finally:
        server.stop()


def test_cli_ftp_download_and_flag_validation(ftp, tmp_path, capsys):
    from personal_download_manager.cli import main

    server, remote, local = ftp
    data = make_content(32 * 1024, seed=506)
    (remote / "cli.bin").write_bytes(data)
    output = local / "cli.bin"

    code = main(
        [
            "download",
            server.url("/cli.bin"),
            "-o",
            str(output),
            "--no-progress",
        ]
    )
    assert code == 0
    assert output.read_bytes() == data

    code = main(["probe", server.url("/cli.bin"), "--json"])
    assert code == 0
    assert '"http_version": "FTP"' in capsys.readouterr().out

    server.modes.clear()
    active = local / "cli-active.bin"
    code = main(
        [
            "download",
            server.url("/cli.bin"),
            "-o",
            str(active),
            "--no-progress",
            "--ftp-active",
        ]
    )
    assert code == 0
    assert active.read_bytes() == data
    assert any(mode in ("PORT", "EPRT") for mode in server.modes)
    assert "PASV" not in server.modes and "EPSV" not in server.modes

    code = main(
        [
            "download",
            server.url("/cli.bin"),
            "-o",
            str(local / "x.bin"),
            "--http2",
        ]
    )
    assert code == 2
    assert "http(s)" in capsys.readouterr().err

    code = main(
        [
            "download",
            "http://127.0.0.1:1/x.bin",
            "-o",
            str(local / "y.bin"),
            "--ftp-active",
        ]
    )
    assert code == 2
    assert "ftp://" in capsys.readouterr().err

    code = main(
        [
            "download",
            "sftp://user@127.0.0.1/x.bin",
            "-o",
            str(local / "z.bin"),
            "--ftp-active",
        ]
    )
    assert code == 2
    assert "ftp://" in capsys.readouterr().err

    code = main(
        ["hls", server.url("/playlist.m3u8"), "-o", str(local / "p.ts")]
    )
    assert code == 2
    assert "http(s)" in capsys.readouterr().err
