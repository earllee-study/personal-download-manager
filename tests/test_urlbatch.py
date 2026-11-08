"""Wildcard batch URL expansion."""

from __future__ import annotations

import pytest

from personal_download_manager.urlbatch import (
    DEFAULT_LIMIT,
    UrlBatchError,
    expand_pattern,
    expand_url_patterns,
    looks_like_url,
)


def test_numeric_range_preserves_zero_padding():
    assert expand_pattern("http://h/img[01-03].jpg") == [
        "http://h/img01.jpg",
        "http://h/img02.jpg",
        "http://h/img03.jpg",
    ]


def test_numeric_range_without_padding_and_with_step():
    assert expand_pattern("http://h/[1-10:4].bin") == [
        "http://h/1.bin",
        "http://h/5.bin",
        "http://h/9.bin",
    ]


def test_character_range():
    assert expand_pattern("http://h/[a-c].txt") == [
        "http://h/a.txt",
        "http://h/b.txt",
        "http://h/c.txt",
    ]


def test_alternatives_and_multiple_tokens():
    assert expand_pattern("http://h/{a,b}/f[1-2].bin") == [
        "http://h/a/f1.bin",
        "http://h/a/f2.bin",
        "http://h/b/f1.bin",
        "http://h/b/f2.bin",
    ]


def test_plain_url_is_returned_unchanged():
    url = "https://example.com/files/report.pdf?token=abc"
    assert expand_pattern(url) == [url]


def test_multiline_input_skips_comments_and_deduplicates():
    text = "\n".join(
        [
            "# comment",
            "",
            "http://h/a[1-2].bin",
            "http://h/a1.bin",       # duplicate expansion
            "http://h/b.bin",
        ]
    )
    assert expand_url_patterns(text) == [
        "http://h/a1.bin",
        "http://h/a2.bin",
        "http://h/b.bin",
    ]


def test_limit_stops_runaway_patterns():
    with pytest.raises(UrlBatchError):
        expand_url_patterns("http://h/[1-100000].bin")
    with pytest.raises(UrlBatchError):
        expand_pattern("http://h/[1-10].bin", limit=5)
    assert len(expand_pattern("http://h/[1-10].bin", limit=10)) == 10
    assert DEFAULT_LIMIT >= 1000


def test_invalid_ranges_are_rejected():
    with pytest.raises(UrlBatchError):
        expand_pattern("http://h/[10-1].bin")
    with pytest.raises(UrlBatchError):
        expand_pattern("http://h/[a-Z].bin")
    with pytest.raises(UrlBatchError):
        expand_pattern("http://h/[1-5:0].bin")


def test_looks_like_url():
    assert looks_like_url("https://example.com/x")
    assert looks_like_url("http://127.0.0.1:8080/x")
    assert looks_like_url("ftp://example.com/x")
    assert looks_like_url("ftps://example.com/x")
    assert looks_like_url("sftp://user@example.com/x")
    assert not looks_like_url("file:///c:/x")
    assert not looks_like_url("not a url")
    assert not looks_like_url("https://")
