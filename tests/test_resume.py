from __future__ import annotations

import re

import pytest

from personal_download_manager.downloader import Downloader
from personal_download_manager.errors import Cancelled
from personal_download_manager.models import DownloadConfig
from server import Route, make_content

RANGE_START = re.compile(r"bytes=(\d+)-")


def _cfg(url, output, **overrides):
    values = dict(
        url=url,
        output=output,
        max_connections=4,
        min_segment_size=1024 * 1024,
        min_split_size=1024 * 1024,
        show_progress=False,
        quiet=True,
        retry_backoff=0.01,
        progress_interval=0.05,
        timeout=10.0,
        connect_timeout=5.0,
        read_timeout=10.0,
    )
    values.update(overrides)
    return DownloadConfig(**values)


def _cancel_after(downloader: Downloader, threshold: int = 1024 * 1024) -> None:
    holder = {"downloader": downloader, "done": False}

    def callback(snapshot):
        if snapshot.downloaded >= threshold and not holder["done"]:
            holder["done"] = True
            holder["downloader"].cancel()

    downloader.callback = callback
    with pytest.raises(Cancelled):
        downloader.run()
    assert holder["done"], "the callback never reached the cancellation threshold"


def _starts_after(server, index: int) -> list[int]:
    starts = []
    for item in server.requests[index:]:
        match = RANGE_START.match(item.get("range") or "")
        if match:
            starts.append(int(match.group(1)))
    return starts


def test_cancel_then_resume_keeps_bytes(server, tmp_path):
    data = make_content(8 * 1024 * 1024, seed=20)
    route = server.add("/resume.bin", Route(content=data, etag='"v1"', throttle_bps=1024 * 1024))
    output = tmp_path / "resume.bin"

    _cancel_after(Downloader(_cfg(server.url("/resume.bin"), output)))
    part = tmp_path / "resume.bin.pdm.part"
    journal = tmp_path / "resume.bin.pdm.json"
    assert part.exists()
    assert journal.exists()
    assert part.stat().st_size == len(data)
    assert not output.exists()

    route.throttle_bps = None  # make the resumed run fast
    mark = len(server.requests)
    result = Downloader(_cfg(server.url("/resume.bin"), output)).run()

    assert output.read_bytes() == data
    assert result.resumed is True
    starts = _starts_after(server, mark)
    assert any(start > 0 for start in starts)
    assert not part.exists()
    assert not journal.exists()


def test_restart_discards_partial_state(server, tmp_path):
    data = make_content(8 * 1024 * 1024, seed=21)
    route = server.add("/restart.bin", Route(content=data, etag='"v1"', throttle_bps=1024 * 1024))
    output = tmp_path / "restart.bin"

    _cancel_after(Downloader(_cfg(server.url("/restart.bin"), output)))
    route.throttle_bps = None

    mark = len(server.requests)
    result = Downloader(
        _cfg(server.url("/restart.bin"), output, restart=True)
    ).run()
    assert output.read_bytes() == data
    assert result.resumed is False
    starts = _starts_after(server, mark)
    assert 0 in starts


def test_changed_etag_forces_a_clean_restart(server, tmp_path):
    data = make_content(8 * 1024 * 1024, seed=22)
    route = server.add("/etag.bin", Route(content=data, etag='"v1"', throttle_bps=1024 * 1024))
    output = tmp_path / "etag.bin"

    _cancel_after(Downloader(_cfg(server.url("/etag.bin"), output)))
    route.throttle_bps = None
    route.etag = '"v2"'

    mark = len(server.requests)
    result = Downloader(_cfg(server.url("/etag.bin"), output)).run()
    assert output.read_bytes() == data
    assert result.resumed is False
    starts = _starts_after(server, mark)
    assert 0 in starts
