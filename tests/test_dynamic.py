from __future__ import annotations

from helpers import run_download
from server import Route, make_content


def _run(server, tmp_path, dynamic: bool):
    data = make_content(8 * 1024 * 1024, seed=50)
    server.add(
        "/dynamic.bin",
        Route(
            content=data,
            etag='"dyn"',
            throttle_bps=2 * 1024 * 1024,
            first_range_fast=True,
        ),
    )
    output = tmp_path / ("dynamic.bin" if dynamic else "static.bin")
    result = run_download(
        server.url("/dynamic.bin"),
        output,
        max_connections=4,
        min_segment_size=1024 * 1024,
        min_split_size=256 * 1024,
        dynamic_segmentation=dynamic,
    )
    assert output.read_bytes() == data
    return result


def test_dynamic_segmentation_splits_live_ranges(server, tmp_path):
    result = _run(server, tmp_path, dynamic=True)
    # Four initial 2 MiB ranges; the fast one finishes first and splits the
    # largest remaining range, keeping all four connections busy.
    assert result.segments_created > 4
    assert result.peak_connections <= 4
    assert result.peak_connections >= 2


def test_dynamic_segmentation_can_be_disabled(server, tmp_path):
    result = _run(server, tmp_path, dynamic=False)
    assert result.segments_created == 4
