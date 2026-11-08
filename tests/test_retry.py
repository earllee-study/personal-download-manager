from __future__ import annotations

from helpers import run_download
from server import Route, make_content


def test_retry_after_transient_503(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=30)
    server.add(
        "/flaky.bin",
        Route(content=data, etag='"f"', fail_range_times=2),
    )
    output = tmp_path / "flaky.bin"

    result = run_download(
        server.url("/flaky.bin"),
        output,
        max_connections=2,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
    )
    assert output.read_bytes() == data
    assert result.retries >= 2


def test_retry_after_dropped_connection(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=31)
    server.add(
        "/drop.bin",
        Route(
            content=data,
            etag='"d"',
            drop_after=300_000,
            drop_times=1,
        ),
    )
    output = tmp_path / "drop.bin"

    result = run_download(
        server.url("/drop.bin"),
        output,
        max_connections=2,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        max_retries=8,
    )
    assert output.read_bytes() == data
    assert result.retries >= 1
