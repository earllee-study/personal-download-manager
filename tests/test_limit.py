from __future__ import annotations

import time

import pytest

from helpers import run_download
from server import Route, make_content


@pytest.mark.slow
def test_speed_limit_is_global(server, tmp_path):
    data = make_content(1024 * 1024, seed=40)
    server.add("/limited.bin", Route(content=data, etag='"l"'))
    output = tmp_path / "limited.bin"

    started = time.monotonic()
    result = run_download(
        server.url("/limited.bin"),
        output,
        max_connections=1,
        min_segment_size=1024 * 1024,
        speed_limit=256 * 1024,
    )
    elapsed = time.monotonic() - started

    assert output.read_bytes() == data
    assert result.total_size == len(data)
    # 1 MiB at 256 KiB/s is 4 s; the first bucket window is free, so allow
    # anything above ~2.5 s to stay robust on a busy machine.
    assert elapsed >= 2.5
