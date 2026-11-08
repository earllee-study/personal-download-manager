"""SFTP: password/key auth, host-key policy, resume, probe and the CLI."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

paramiko = pytest.importorskip("paramiko")

from sftpserver import SftpServer  # noqa: E402
from server import make_content  # noqa: E402

from personal_download_manager.downloader import Downloader  # noqa: E402
from personal_download_manager.errors import (  # noqa: E402
    AuthRequired,
    Cancelled,
    ChecksumMismatch,
    FatalError,
    ProbeError,
)
from personal_download_manager.models import DownloadConfig  # noqa: E402
from personal_download_manager.sftp import probe_sftp  # noqa: E402


@pytest.fixture
def sftp(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    server = SftpServer(remote).start()
    try:
        yield server, remote, local
    finally:
        server.stop()


def _config(server, url_path: str, output, **overrides) -> DownloadConfig:
    defaults = dict(
        url=server.url(url_path),
        output=output,
        sftp_auto_host_key=True,
        verify=False,
        show_progress=False,
        quiet=True,
        timeout=15.0,
        connect_timeout=10.0,
        read_timeout=15.0,
        max_retries=2,
    )
    defaults.update(overrides)
    return DownloadConfig(**defaults)


def test_sftp_download_is_byte_exact(sftp):
    server, remote, local = sftp
    data = make_content(512 * 1024, seed=601)
    (remote / "file.bin").write_bytes(data)
    output = local / "file.bin"

    result = Downloader(_config(server, "/file.bin", output)).run()

    assert output.read_bytes() == data
    assert result.total_size == len(data)
    assert result.range_supported is False
    assert not (local / "file.bin.pdm.part").exists()
    assert not (local / "file.bin.pdm.json").exists()


def test_sftp_probe_reports_size(sftp):
    server, remote, _local = sftp
    data = make_content(64 * 1024, seed=602)
    (remote / "probe.bin").write_bytes(data)

    result = probe_sftp(_config(server, "/probe.bin", None))

    assert result.total_size == len(data)
    assert result.filename == "probe.bin"
    assert result.http_version == "SFTP"
    assert result.last_modified is not None


def test_sftp_resumes_after_cancel(sftp):
    server, remote, local = sftp
    data = make_content(1024 * 1024, seed=603)
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
    assert (local / "resume.bin.pdm.part").exists()

    result = Downloader(_config(server, "/resume.bin", output)).run()
    assert result.resumed is True
    assert output.read_bytes() == data


def test_sftp_wrong_password_is_fatal(sftp):
    server, remote, _local = sftp
    (remote / "file.bin").write_bytes(b"x")
    config = replace(
        _config(server, "/file.bin", None),
        url=server.url("/file.bin", password="wrong"),
    )
    with pytest.raises(AuthRequired):
        Downloader(config).run()


def test_sftp_unknown_host_key_is_rejected(sftp, tmp_path):
    server, remote, _local = sftp
    (remote / "file.bin").write_bytes(b"x")
    empty = tmp_path / "known_hosts"
    empty.write_text("", encoding="utf-8")
    config = _config(
        server,
        "/file.bin",
        None,
        sftp_auto_host_key=False,
        sftp_known_hosts=str(empty),
    )
    with pytest.raises(ProbeError) as excinfo:
        Downloader(config).run()
    message = str(excinfo.value).lower()
    assert "host key" in message
    assert "--sftp-auto-host-key" in str(excinfo.value)


def test_sftp_known_hosts_file_is_accepted(sftp, tmp_path):
    server, remote, local = sftp
    data = make_content(32 * 1024, seed=604)
    (remote / "kh.bin").write_bytes(data)
    known = tmp_path / "known_hosts"
    known.write_text(server.known_hosts(), encoding="utf-8")
    output = local / "kh.bin"

    config = _config(
        server,
        "/kh.bin",
        output,
        sftp_auto_host_key=False,
        sftp_known_hosts=str(known),
    )
    Downloader(config).run()
    assert output.read_bytes() == data


def test_sftp_uses_auth_option_for_password(sftp):
    server, remote, local = sftp
    data = make_content(32 * 1024, seed=608)
    (remote / "auth.bin").write_bytes(data)
    output = local / "auth.bin"
    config = replace(
        _config(server, "/auth.bin", output),
        url=f"sftp://user@127.0.0.1:{server.port}/auth.bin",
        auth=("user", "pw"),
    )
    Downloader(config).run()
    assert output.read_bytes() == data


def test_sftp_private_key_auth(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    data = make_content(32 * 1024, seed=605)
    (remote / "key.bin").write_bytes(data)
    client_key = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "id_rsa"
    client_key.write_private_key_file(str(key_path))
    server = SftpServer(remote, password=None, allowed_key=client_key).start()
    try:
        output = tmp_path / "key.bin"
        config = DownloadConfig(
            url=server.url("/key.bin", password=None),
            output=output,
            sftp_auto_host_key=True,
            sftp_key=str(key_path),
            verify=False,
            show_progress=False,
            quiet=True,
            timeout=15.0,
            connect_timeout=10.0,
            read_timeout=15.0,
        )
        Downloader(config).run()
        assert output.read_bytes() == data
    finally:
        server.stop()


def test_sftp_missing_file_fails(sftp):
    server, _remote, local = sftp
    with pytest.raises(ProbeError) as excinfo:
        Downloader(_config(server, "/missing.bin", local / "missing.bin")).run()
    assert "not found" in str(excinfo.value).lower()


def test_sftp_sha256_is_verified(sftp):
    server, remote, local = sftp
    data = make_content(32 * 1024, seed=606)
    (remote / "sum.bin").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    output = local / "sum.bin"
    result = Downloader(
        _config(server, "/sum.bin", output, sha256=digest)
    ).run()
    assert result.sha256 == digest

    with pytest.raises(ChecksumMismatch):
        Downloader(
            _config(server, "/sum.bin", local / "bad.bin", sha256="0" * 64)
        ).run()


def test_sftp_rejects_proxy(sftp):
    server, remote, local = sftp
    (remote / "file.bin").write_bytes(b"x")
    with pytest.raises(FatalError, match="proxy"):
        Downloader(
            _config(
                server,
                "/file.bin",
                local / "x.bin",
                proxy="http://127.0.0.1:8080",
            )
        ).run()


def test_cli_sftp_download_and_probe(sftp, tmp_path, capsys):
    from personal_download_manager.cli import main

    server, remote, local = sftp
    data = make_content(16 * 1024, seed=607)
    (remote / "cli.bin").write_bytes(data)
    output = local / "cli.bin"

    code = main(
        [
            "download",
            server.url("/cli.bin"),
            "-o",
            str(output),
            "--sftp-auto-host-key",
            "--no-progress",
        ]
    )
    assert code == 0
    assert output.read_bytes() == data

    code = main(
        [
            "probe",
            server.url("/cli.bin"),
            "--sftp-auto-host-key",
            "--json",
        ]
    )
    assert code == 0
    assert '"http_version": "SFTP"' in capsys.readouterr().out

    code = main(
        [
            "download",
            server.url("/cli.bin"),
            "-o",
            str(local / "y.bin"),
            "--proxy",
            "http://127.0.0.1:8080",
        ]
    )
    assert code == 2
    assert "proxy" in capsys.readouterr().err
