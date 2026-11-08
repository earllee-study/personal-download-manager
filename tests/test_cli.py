from __future__ import annotations

import json

import pytest

from personal_download_manager.cli import main
from server import Route, make_content


def test_probe_cli_json(server, capsys):
    data = make_content(64 * 1024, seed=60)
    server.add("/cli.bin", Route(content=data, etag='"cli"'))
    code = main(["probe", server.url("/cli.bin"), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_size"] == len(data)
    assert payload["ranges"] is True


def test_download_cli(server, tmp_path):
    data = make_content(1024 * 1024, seed=61)
    server.add("/cli.bin", Route(content=data, etag='"cli"'))
    output = tmp_path / "cli.bin"
    code = main(
        [
            "download",
            server.url("/cli.bin"),
            "-o",
            str(output),
            "-n",
            "4",
            "--no-progress",
            "--retries",
            "1",
            "--retry-delay",
            "0.01",
        ]
    )
    assert code == 0
    assert output.read_bytes() == data


def test_invalid_header_is_rejected(server):
    with pytest.raises(SystemExit):
        main(["download", server.url("/anything"), "-H", "not-a-header"])


def test_version_exits_zero():
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
