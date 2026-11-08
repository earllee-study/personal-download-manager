from __future__ import annotations

import pytest

from personal_download_manager.errors import (
    AuthRequired,
    ChecksumMismatch,
    FatalError,
    HTTPStatusError,
    ProtocolError,
)
from helpers import run_download
from server import Route, make_content, sha256_bytes


def test_multi_segment_download_is_byte_exact(server, tmp_path):
    data = make_content(8 * 1024 * 1024, seed=10)
    server.add("/file.bin", Route(content=data, etag='"v1"'))
    output = tmp_path / "file.bin"

    result = run_download(
        server.url("/file.bin"),
        output,
        max_connections=8,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
    )

    assert output.read_bytes() == data
    assert result.total_size == len(data)
    assert result.segments_created >= 8
    assert result.peak_connections >= 2
    assert result.resumed is False
    assert result.retries == 0

    ranges = [
        item
        for item in server.requests
        if item["range"] and item["range"] != "bytes=0-0"
    ]
    assert len(ranges) >= 8
    assert all(item["status"] == 206 for item in ranges)
    assert not (tmp_path / "file.bin.pdm.part").exists()
    assert not (tmp_path / "file.bin.pdm.json").exists()


def test_server_without_range_support(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=11)
    server.add("/plain.bin", Route(content=data, ignore_range=True))
    output = tmp_path / "plain.bin"

    result = run_download(
        server.url("/plain.bin"),
        output,
        max_connections=8,
        min_segment_size=512 * 1024,
    )

    assert output.read_bytes() == data
    assert result.range_supported is False
    assert result.segments_created == 1
    download_requests = [
        item
        for item in server.requests
        if item["method"] == "GET" and item["range"] is None
    ]
    assert len(download_requests) == 1


def test_chunked_response_without_length(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=12)
    server.add(
        "/chunked.bin",
        Route(content=data, ignore_range=True, chunked=True),
    )
    output = tmp_path / "chunked.bin"

    result = run_download(server.url("/chunked.bin"), output)
    assert output.read_bytes() == data
    assert result.total_size == len(data)
    assert result.range_supported is False


def test_content_disposition_names_the_file(server, tmp_path, monkeypatch):
    data = make_content(1024 * 1024, seed=13)
    server.add(
        "/named",
        Route(
            content=data,
            content_disposition=(
                "attachment; filename*=UTF-8''%E4%B8%AD%E6%96%87%20file.bin"
            ),
        ),
    )
    monkeypatch.chdir(tmp_path)
    result = run_download(server.url("/named"), None)
    assert result.path.name == "中文 file.bin"
    assert result.path.read_bytes() == data


def test_redirect_is_followed(server, tmp_path):
    data = make_content(1024 * 1024, seed=14)
    server.add("/real.bin", Route(content=data, etag='"r"'))
    server.add("/go", Route(redirect_to="/real.bin"))
    output = tmp_path / "real.bin"
    result = run_download(server.url("/go"), output)
    assert result.final_url.endswith("/real.bin")
    assert output.read_bytes() == data


def test_basic_auth(server, tmp_path):
    data = make_content(512 * 1024, seed=15)
    server.add(
        "/private.bin",
        Route(content=data, require_auth=("alice", "s3cret")),
    )
    output = tmp_path / "private.bin"

    with pytest.raises(AuthRequired):
        run_download(server.url("/private.bin"), output)

    result = run_download(
        server.url("/private.bin"), output, auth=("alice", "s3cret")
    )
    assert output.read_bytes() == data
    assert result.total_size == len(data)


def test_empty_file(server, tmp_path):
    server.add("/empty.bin", Route(content=b""))
    output = tmp_path / "empty.bin"
    result = run_download(server.url("/empty.bin"), output)
    assert output.exists()
    assert output.stat().st_size == 0
    assert result.total_size == 0


def test_checksum_verification(server, tmp_path):
    data = make_content(512 * 1024, seed=16)
    server.add("/sum.bin", Route(content=data, etag='"s"'))
    output = tmp_path / "sum.bin"
    expected = sha256_bytes(data)

    result = run_download(
        server.url("/sum.bin"), output, sha256=expected
    )
    assert result.sha256 == expected
    assert output.read_bytes() == data

    bad_output = tmp_path / "bad.bin"
    with pytest.raises(ChecksumMismatch):
        run_download(
            server.url("/sum.bin"),
            bad_output,
            sha256="0" * 64,
        )
    # The verified file is not published under the final name.
    assert not bad_output.exists()


def test_missing_url_is_a_probe_error(server, tmp_path):
    with pytest.raises(HTTPStatusError) as info:
        run_download(server.url("/nope.bin"), tmp_path / "nope.bin")
    assert info.value.status == 404


def test_206_without_content_range_is_rejected(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=17)
    server.add(
        "/bad-range.bin",
        Route(content=data, etag='"bad"', omit_content_range=True),
    )
    with pytest.raises(ProtocolError):
        run_download(
            server.url("/bad-range.bin"),
            tmp_path / "bad-range.bin",
            max_connections=4,
            min_segment_size=512 * 1024,
        )


def test_encoded_body_is_rejected(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=18)
    server.add(
        "/encoded.bin",
        Route(content=data, etag='"enc"', content_encoding="gzip"),
    )
    with pytest.raises(FatalError):
        run_download(
            server.url("/encoded.bin"),
            tmp_path / "encoded.bin",
            max_connections=4,
            min_segment_size=512 * 1024,
        )
