"""AIMD connection control and the shared HTTP/2 client path."""

from __future__ import annotations

import time

import pytest

from helpers import run_download
from personal_download_manager.adaptive import AimdController
from server import Route, make_content


# ----------------------------------------------------------------------
# controller
# ----------------------------------------------------------------------
def test_aimd_adds_one_connection_per_interval():
    controller = AimdController(minimum=1, maximum=5, start=2, interval=1.0)
    assert controller.observe(100.0, 1024, 0.2) == 2  # first sample sets the clock
    assert controller.observe(100.5, 1024, 0.2) == 2
    assert controller.observe(101.1, 1024, 0.2) == 3
    assert controller.observe(102.2, 1024, 0.2) == 4
    assert controller.observe(103.3, 1024, 0.2) == 5
    assert controller.observe(104.4, 1024, 0.2) == 5  # capped


def test_aimd_halves_on_pressure_and_cools_down():
    controller = AimdController(
        minimum=1, maximum=8, start=8, interval=1.0, cooldown=3.0
    )
    assert controller.on_pressure(10.0) == 4
    assert controller.on_pressure(10.1) == 2
    assert controller.on_pressure(10.2) == 1
    assert controller.on_pressure(10.3) == 1  # floor
    # No increase during the cooldown...
    assert controller.observe(12.9, 1024, 0.2) == 1
    # ...and one afterwards (interval and cooldown both elapsed).
    assert controller.observe(13.5, 1024, 0.2) == 2


def test_aimd_ignores_idle_samples():
    controller = AimdController(minimum=1, maximum=8, start=2, interval=0.5)
    controller.observe(1.0, 0, 0.2)      # sets clock, no increase
    assert controller.observe(3.0, 0, 0.2) == 2  # no bytes -> no increase
    assert controller.observe(4.0, 100, 0.0) == 2  # zero elapsed -> ignore


# ----------------------------------------------------------------------
# engine integration
# ----------------------------------------------------------------------
def test_adaptive_ramps_up_connections(server, tmp_path):
    data = make_content(16 * 1024 * 1024, seed=130)
    server.add(
        "/ramp.bin",
        Route(content=data, etag='"ramp"', throttle_bps=4 * 1024 * 1024),
    )
    started = time.monotonic()
    result = run_download(
        server.url("/ramp.bin"),
        tmp_path / "ramp.bin",
        max_connections=8,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        adaptive=True,
        adaptive_start=2,
        adaptive_interval=0.15,
    )
    elapsed = time.monotonic() - started

    assert (tmp_path / "ramp.bin").read_bytes() == data
    assert result.peak_connections >= 3, "AIMD should add connections"
    assert result.final_connections >= 4, result.final_connections
    assert result.final_connections <= 8
    assert elapsed < 30


def test_adaptive_backs_off_on_503(server, tmp_path):
    data = make_content(8 * 1024 * 1024, seed=131)
    server.add(
        "/pushback.bin",
        Route(
            content=data,
            etag='"pushback"',
            throttle_bps=4 * 1024 * 1024,
            fail_range_times=6,
        ),
    )
    result = run_download(
        server.url("/pushback.bin"),
        tmp_path / "pushback.bin",
        max_connections=6,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        adaptive=True,
        adaptive_start=4,
        adaptive_interval=0.5,
        max_retries=8,
    )

    assert (tmp_path / "pushback.bin").read_bytes() == data
    assert result.final_connections < 4, "503s must shrink the budget"
    assert result.peak_connections >= 1


def test_http2_client_is_shared_and_still_correct(server, tmp_path):
    pytest.importorskip("h2")
    data = make_content(4 * 1024 * 1024, seed=132)
    server.add("/h2.bin", Route(content=data, etag='"h2"'))
    result = run_download(
        server.url("/h2.bin"),
        tmp_path / "h2.bin",
        max_connections=4,
        min_segment_size=512 * 1024,
        min_split_size=512 * 1024,
        http2=True,
    )
    assert (tmp_path / "h2.bin").read_bytes() == data
    # The test server speaks HTTP/1.1, so the shared client's pool opens one
    # connection per stream; the point is that the shared-client path works.
    assert result.peak_connections >= 2
