from __future__ import annotations

import pytest

from personal_download_manager.errors import AuthRequired, HTTPStatusError
from personal_download_manager.models import DownloadConfig
from personal_download_manager.probe import probe
from server import Route, make_content


def cfg(url: str, **kwargs) -> DownloadConfig:
    return DownloadConfig(
        url=url,
        show_progress=False,
        quiet=True,
        timeout=5.0,
        connect_timeout=3.0,
        read_timeout=5.0,
        **kwargs,
    )


def test_probe_detects_ranges_and_metadata(server):
    data = make_content(2 * 1024 * 1024, seed=1)
    server.add(
        "/file.bin",
        Route(content=data, etag='"v1"', content_type="application/octet-stream"),
    )
    result = probe(cfg(server.url("/file.bin")))
    assert result.status == 206
    assert result.ranges is True
    assert result.total_size == len(data)
    assert result.etag == '"v1"'
    assert result.filename == "file.bin"


def test_probe_ignored_range_reports_single_stream(server):
    data = make_content(1024 * 1024, seed=2)
    server.add("/plain.bin", Route(content=data, ignore_range=True))
    result = probe(cfg(server.url("/plain.bin")))
    assert result.status == 200
    assert result.ranges is False
    assert result.total_size == len(data)


def test_probe_follows_redirect(server):
    data = make_content(4096, seed=3)
    server.add("/target.bin", Route(content=data, etag='"t"'))
    server.add("/redir", Route(redirect_to="/target.bin"))
    result = probe(cfg(server.url("/redir")))
    assert result.final_url.endswith("/target.bin")
    assert result.total_size == len(data)
    assert result.filename == "target.bin"


def test_probe_content_disposition_filename(server):
    data = make_content(4096, seed=4)
    server.add(
        "/named",
        Route(
            content=data,
            content_disposition=(
                "attachment; filename=fallback.bin; "
                "filename*=UTF-8''%E4%B8%AD%E6%96%87%20file.bin"
            ),
        ),
    )
    result = probe(cfg(server.url("/named")))
    assert result.filename == "中文 file.bin"


def test_probe_head_fallback_when_get_range_rejected(server):
    data = make_content(8192, seed=5)
    server.add("/no-get-range", Route(content=data, reject_range_requests=True))
    result = probe(cfg(server.url("/no-get-range")))
    assert result.status == 200
    assert result.total_size == len(data)
    assert result.ranges is True  # Accept-Ranges from the HEAD response


def test_probe_auth_required(server):
    server.add(
        "/private.bin",
        Route(content=b"secret", require_auth=("user", "pass")),
    )
    with pytest.raises(AuthRequired):
        probe(cfg(server.url("/private.bin")))

    result = probe(cfg(server.url("/private.bin"), auth=("user", "pass")))
    assert result.total_size == 6


def test_probe_404(server):
    with pytest.raises(HTTPStatusError) as info:
        probe(cfg(server.url("/missing.bin")))
    assert info.value.status == 404


def test_probe_empty_file(server):
    server.add("/empty.bin", Route(content=b"", etag='"empty"'))
    result = probe(cfg(server.url("/empty.bin")))
    assert result.status == 416
    assert result.total_size == 0
    assert result.ranges is True
