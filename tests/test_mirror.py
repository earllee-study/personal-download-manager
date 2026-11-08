"""Multi-source mirrors: compatibility checks and segment spreading."""

from __future__ import annotations

import pytest

from helpers import run_download
from personal_download_manager.models import ProbeResult, Segment
from personal_download_manager.mirror import MirrorPool, mirrors_compatible
from server import Route, TestServer as _TestServer, make_content


def probe(url: str, size: int, etag: str | None = None, last_modified=None) -> ProbeResult:
    return ProbeResult(
        url=url,
        final_url=url,
        status=206,
        total_size=size,
        ranges=True,
        etag=etag,
        last_modified=last_modified,
    )


def test_mirror_compatibility_rules():
    primary = probe("http://a/f", 100, etag='"same"')
    assert mirrors_compatible(primary, probe("http://b/f", 100, etag='"same"'))
    assert not mirrors_compatible(primary, probe("http://b/f", 100, etag='"other"'))
    assert not mirrors_compatible(primary, probe("http://b/f", 99, etag='"same"'))
    no_ranges = probe("http://b/f", 100, etag='"same"')
    no_ranges.ranges = False
    assert not mirrors_compatible(primary, no_ranges)

    by_date = probe("http://a/f", 100, last_modified="Wed, 21 Oct 2015 07:28:00 GMT")
    assert mirrors_compatible(
        by_date,
        probe("http://b/f", 100, last_modified="Wed, 21 Oct 2015 07:28:00 GMT"),
    )
    assert not mirrors_compatible(
        by_date,
        probe("http://b/f", 100, last_modified="Thu, 22 Oct 2015 07:28:00 GMT"),
    )


def test_mirror_pool_round_robin_and_failover():
    pool = MirrorPool(probe("http://a/f", 100), [probe("http://b/f", 100)])
    assert pool.count == 2
    assert [pool.source_for(i) for i in range(4)] == [0, 1, 0, 1]
    segment = Segment(index=0, start=0, end=10, source=1)
    assert pool.url_for(segment) == "http://b/f"
    assert pool.failover(segment) is True
    assert segment.source == 0
    assert pool.url_for(segment) == "http://a/f"
    assert pool.failover(segment) is False  # everything is dead now


def test_mirror_pool_remap_handles_old_journals():
    pool = MirrorPool(probe("http://a/f", 100), [probe("http://b/f", 100)])
    segments = [Segment(index=0, start=0, end=10, source=7)]
    pool.remap(segments)
    assert segments[0].source == 1


def test_download_uses_both_mirrors(server, tmp_path):
    data = make_content(4 * 1024 * 1024, seed=140)
    server.add("/mirror.bin", Route(content=data, etag='"mirror"'))
    secondary = _TestServer().start()
    try:
        secondary.add("/mirror.bin", Route(content=data, etag='"mirror"'))
        result = run_download(
            server.url("/mirror.bin"),
            tmp_path / "mirror.bin",
            mirrors=[secondary.url("/mirror.bin")],
            max_connections=4,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
    finally:
        secondary.stop()

    assert (tmp_path / "mirror.bin").read_bytes() == data
    assert result.segments_created >= 4

    def ranged(srv):
        return [
            item
            for item in srv.requests
            if item["range"] and item["range"] != "bytes=0-0"
        ]

    primary_ranges = ranged(server)
    mirror_ranges = ranged(secondary)
    assert primary_ranges, "primary should still serve part of the file"
    assert mirror_ranges, "the mirror should serve part of the file"
    assert all(item["status"] == 206 for item in primary_ranges + mirror_ranges)


def test_mismatched_mirror_is_ignored(server, tmp_path):
    data = make_content(1024 * 1024, seed=141)
    server.add("/same.bin", Route(content=data, etag='"v1"'))
    secondary = _TestServer().start()
    try:
        secondary.add("/same.bin", Route(content=data, etag='"v2"'))
        run_download(
            server.url("/same.bin"),
            tmp_path / "same.bin",
            mirrors=[secondary.url("/same.bin")],
            max_connections=4,
            min_segment_size=256 * 1024,
        )
    finally:
        secondary.stop()

    assert (tmp_path / "same.bin").read_bytes() == data
    assert not [
        item
        for item in secondary.requests
        if item["range"] and item["range"] != "bytes=0-0"
    ]


def test_mirror_without_range_support_is_ignored(server, tmp_path):
    data = make_content(1024 * 1024, seed=142)
    server.add("/plain2.bin", Route(content=data, etag='"plain2"'))
    secondary = _TestServer().start()
    try:
        secondary.add(
            "/plain2.bin",
            Route(content=data, etag='"plain2"', ignore_range=True),
        )
        run_download(
            server.url("/plain2.bin"),
            tmp_path / "plain2.bin",
            mirrors=[secondary.url("/plain2.bin")],
            max_connections=4,
            min_segment_size=256 * 1024,
        )
    finally:
        secondary.stop()

    assert (tmp_path / "plain2.bin").read_bytes() == data
    assert not [
        item
        for item in secondary.requests
        if item["range"] and item["range"] != "bytes=0-0"
    ]


def test_unreachable_mirror_is_ignored(server, tmp_path):
    data = make_content(512 * 1024, seed=143)
    server.add("/ok.bin", Route(content=data, etag='"ok"'))
    run_download(
        server.url("/ok.bin"),
        tmp_path / "ok.bin",
        mirrors=["http://127.0.0.1:9/dead.bin"],
        max_connections=2,
        min_segment_size=128 * 1024,
        connect_timeout=1.0,
        timeout=5.0,
    )
    assert (tmp_path / "ok.bin").read_bytes() == data
