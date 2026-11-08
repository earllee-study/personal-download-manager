from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_download_manager.headers import (
    format_eta,
    format_size,
    is_strong_etag,
    parse_content_disposition,
    parse_content_range,
    parse_cookie_header,
    parse_retry_after,
    parse_size,
    parse_unsatisfied_range,
    sanitize_filename,
    unique_path,
)


def test_content_range_parsing():
    cr = parse_content_range("bytes 0-1023/4096")
    assert cr is not None
    assert (cr.start, cr.end, cr.total, cr.length) == (0, 1023, 4096, 1024)
    assert parse_content_range("bytes 0-0/*").total is None
    assert parse_content_range("bytes 5-4/100") is None
    assert parse_content_range("garbage") is None
    assert parse_unsatisfied_range("bytes */1234") == 1234
    assert parse_unsatisfied_range("bytes 0-1/2") is None


def test_retry_after_seconds_and_date():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("not-a-date") is None
    now = datetime.now(timezone.utc)
    when = (now + timedelta(seconds=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    value = parse_retry_after(when, now=now)
    assert value is not None and 18 <= value <= 21


def test_content_disposition_plain_and_rfc5987():
    assert (
        parse_content_disposition('attachment; filename="report.pdf"')
        == "report.pdf"
    )
    assert (
        parse_content_disposition(
            "attachment; filename=fallback.bin; "
            "filename*=UTF-8''%E4%B8%AD%E6%96%87.zip"
        )
        == "中文.zip"
    )
    # Quoted parameter with a semicolon inside.
    assert (
        parse_content_disposition('attachment; filename="a;b.bin"') == "a;b.bin"
    )
    assert parse_content_disposition(None) is None


def test_content_disposition_recovers_utf8_mojibake():
    mojibake = "中文.pdf".encode("utf-8").decode("latin-1")
    assert (
        parse_content_disposition(f'attachment; filename="{mojibake}"')
        == "中文.pdf"
    )


def test_etag_strength():
    assert is_strong_etag('"abc"')
    assert not is_strong_etag('W/"abc"')
    assert not is_strong_etag(None)


def test_sanitize_filename_windows_rules():
    assert sanitize_filename('a<b>c:"d"/e\\f|g?h*.txt') == "a_b_c__d__e_f_g_h_.txt"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("name. ") == "name"
    assert sanitize_filename("") == "download"
    long_name = "x" * 500 + ".bin"
    cleaned = sanitize_filename(long_name)
    assert len(cleaned) <= 180
    assert cleaned.endswith(".bin")


def test_unique_path(tmp_path: Path):
    first = tmp_path / "file.bin"
    first.write_bytes(b"x")
    second = unique_path(first)
    assert second.name == "file (1).bin"
    second.write_bytes(b"x")
    assert unique_path(first).name == "file (2).bin"


def test_parse_size_and_cookies():
    assert parse_size("10") == 10
    assert parse_size("1K") == 1024
    assert parse_size("1.5M") == 1024 * 1024 * 3 // 2
    assert parse_size("2G") == 2 * 1024**3
    assert parse_size(None) is None
    assert parse_cookie_header("a=b; c=d=e; broken") == {"a": "b", "c": "d=e"}


def test_formatters():
    assert format_size(0) == "0 B"
    assert format_size(1536) == "1.5 KiB"
    assert format_eta(None) == "--:--"
    assert format_eta(65) == "01:05"
    assert format_eta(3661) == "1:01:01"
