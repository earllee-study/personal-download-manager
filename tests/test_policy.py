"""Per-host connection caps, per-host limits and the global speed limit."""

from __future__ import annotations

import threading
import time

import pytest

from helpers import run_download
from personal_download_manager.policy import (
    ConnectionLimiter,
    Governor,
    HostPolicies,
    HostRule,
    host_of,
    normalize_proxy_groups,
)
from server import Route, make_content


# ----------------------------------------------------------------------
# policy resolution
# ----------------------------------------------------------------------
def test_host_matching_and_precedence():
    policies = HostPolicies(
        [
            HostRule("example.com", connections=2, proxy="http://p1:8080"),
            HostRule("*.example.com", connections=4),
            HostRule("slow.test", speed_limit="1M"),
        ],
        default_connections=8,
    )
    exact = policies.resolve("https://example.com/file")
    assert exact.connections == 2
    assert exact.proxy == "http://p1:8080"

    sub = policies.resolve("https://api.example.com/file")
    assert sub.connections == 4
    assert sub.proxy is None

    other = policies.resolve("https://elsewhere.org/file")
    assert other.connections == 8

    slow = policies.resolve("slow.test")
    assert slow.connections == 8  # falls back to defaults
    assert slow.speed_limit == 1024 * 1024

    # Bare host names and case-insensitive matching.
    assert host_of("HTTPS://Example.COM/x") == "example.com"
    assert policies.resolve("EXAMPLE.COM").connections == 2


def test_first_matching_rule_wins():
    policies = HostPolicies(
        [HostRule("*", connections=1), HostRule("example.com", connections=6)]
    )
    assert policies.resolve("example.com").connections == 1


def test_rule_and_policy_validation():
    with pytest.raises(ValueError):
        HostRule("  ", connections=2)
    with pytest.raises(ValueError):
        HostRule("x", connections=-1)
    with pytest.raises(ValueError):
        HostRule("x", speed_limit=-5)
    assert HostRule("x", connections=999).connections == 64  # clamped


def test_proxy_groups_share_one_proxy_across_rules():
    policies = HostPolicies(
        [
            HostRule("grouped.test", proxy_group="us"),
            HostRule("explicit.test", proxy="http://direct:1", proxy_group="us"),
            HostRule("unknown.test", proxy_group="ghost"),
        ],
        default_proxy="http://default:3128",
        proxy_groups={"us": "socks5://us:1080"},
    )
    # Group used, explicit proxy wins over a group, unknown group -> default.
    assert policies.resolve("https://grouped.test/").proxy == "socks5://us:1080"
    assert policies.resolve("https://explicit.test/").proxy == "http://direct:1"
    assert policies.resolve("https://unknown.test/").proxy == "http://default:3128"
    assert policies.resolve("https://elsewhere.test/").proxy == "http://default:3128"
    assert policies.unknown_groups() == ["ghost"]

    # Moving a site to another proxy is a single group edit.
    moved = HostPolicies(
        policies.rules,
        default_proxy="http://default:3128",
        proxy_groups={"us": "socks5://eu:2080"},
    )
    assert moved.resolve("https://grouped.test/").proxy == "socks5://eu:2080"


def test_proxy_groups_accept_list_and_mapping_forms():
    from_map = normalize_proxy_groups({"us": "socks5://u:1"})
    assert from_map == {"us": "socks5://u:1"}
    assert normalize_proxy_groups(
        [
            {"name": " a ", "proxy": "http://p:1"},
            {"name": "a", "proxy": "http://ignored:2"},
            {"name": "", "proxy": "http://x"},
            {"name": "b", "proxy": ""},
            ["c", "http://c:3"],
            42,
        ]
    ) == {"a": "http://p:1", "c": "http://c:3"}


def test_proxy_groups_dict_roundtrip():
    policies = HostPolicies(
        [HostRule("*.cdn.test", proxy_group="fast")],
        proxy_groups=[{"name": "fast", "proxy": "socks5://f:1"}],
    )
    data = policies.to_dict()
    assert data["proxy_groups"] == {"fast": "socks5://f:1"}
    assert data["hosts"][0]["proxy_group"] == "fast"
    restored = HostPolicies.from_dict(data)
    assert restored.resolve("https://a.cdn.test/file").proxy == "socks5://f:1"


def test_cli_policy_file_supports_proxy_groups(tmp_path):
    import argparse
    import json

    from personal_download_manager.cli import _governor_from_args

    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "defaults": {"proxy": "http://default:3128"},
                "proxy_groups": {"us": "socks5://us:1080"},
                "hosts": [
                    {"pattern": "members.example.com", "proxy_group": "us"},
                ],
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        policy=str(path), max_host_connections=4, global_limit=None
    )
    governor = _governor_from_args(args)
    assert governor.resolve("https://members.example.com/x").proxy == "socks5://us:1080"
    assert governor.resolve("https://members.example.com/x").connections == 4
    # The --max-host-connections rebuild must keep the groups.
    assert governor.policies.proxy_groups == {"us": "socks5://us:1080"}


def test_policy_dict_roundtrip():
    policies = HostPolicies(
        [HostRule("*.cdn.test", connections=3, speed_limit="2M", proxy="socks5://h:1")],
        default_connections=6,
    )
    restored = HostPolicies.from_dict(policies.to_dict())
    assert restored.default_connections == 6
    resolved = restored.resolve("https://a.cdn.test/file")
    assert resolved.connections == 3
    assert resolved.speed_limit == 2 * 1024 * 1024
    assert resolved.proxy == "socks5://h:1"


# ----------------------------------------------------------------------
# connection limiter
# ----------------------------------------------------------------------
def test_connection_limiter_blocks_over_cap():
    limiter = ConnectionLimiter(HostPolicies(default_connections=1))
    assert limiter.acquire("https://h.test")
    outcome: dict = {}

    def second() -> None:
        started = time.monotonic()
        outcome["ok"] = limiter.acquire("https://h.test")
        outcome["waited"] = time.monotonic() - started
        limiter.release("https://h.test")

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive(), "second acquire must wait for the slot"
    limiter.release("https://h.test")
    thread.join(2)
    assert outcome["ok"] is True
    assert outcome["waited"] >= 0.15


def test_connection_limiter_gives_up_on_cancel():
    limiter = ConnectionLimiter(HostPolicies(default_connections=1))
    assert limiter.acquire("h.test")
    cancelled = threading.Event()
    cancelled.set()
    assert limiter.acquire("h.test", cancelled) is False
    limiter.release("h.test")


# ----------------------------------------------------------------------
# governor
# ----------------------------------------------------------------------
def test_governor_global_bucket_paces():
    governor = Governor(global_speed_limit=256 * 1024)
    started = time.monotonic()
    for _ in range(4):  # 1 MiB total, 256 KiB burst, then 256 KiB/s
        governor.consume(256 * 1024)
    elapsed = time.monotonic() - started
    assert elapsed >= 1.4, elapsed


# ----------------------------------------------------------------------
# engine integration
# ----------------------------------------------------------------------
def _run_in_thread(url, output, errors, **kwargs):
    thread = threading.Thread(
        target=lambda: _guarded(errors, url, output, kwargs), daemon=True
    )
    thread.start()
    return thread


def _guarded(errors, url, output, kwargs):
    try:
        run_download(url, output, **kwargs)
    except BaseException as exc:  # surfaced by the test
        errors.append(exc)


def test_per_host_cap_is_shared_across_downloads(server, tmp_path):
    data = make_content(2 * 1024 * 1024, seed=120)
    server.add("/share.bin", Route(content=data, etag='"share"', throttle_bps=2 * 1024 * 1024))
    governor = Governor(HostPolicies(default_connections=2))
    errors: list = []
    url = server.url("/share.bin")
    threads = [
        _run_in_thread(
            url,
            tmp_path / f"part-{index}.bin",
            errors,
            governor=governor,
            max_connections=4,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
        for index in range(2)
    ]

    peak = 0
    while any(thread.is_alive() for thread in threads):
        peak = max(peak, governor.limiter.active(url))
        time.sleep(0.005)
    for thread in threads:
        thread.join(5)

    assert not errors, errors
    assert peak == 2, f"expected the shared cap of 2, saw {peak}"
    for index in range(2):
        assert (tmp_path / f"part-{index}.bin").read_bytes() == data


def test_global_limit_spans_two_downloads(server, tmp_path):
    size = 512 * 1024
    data = make_content(size, seed=121)
    server.add("/g.bin", Route(content=data, etag='"g"'))
    governor = Governor(global_speed_limit=256 * 1024)
    errors: list = []
    url = server.url("/g.bin")

    started = time.monotonic()
    threads = [
        _run_in_thread(
            url,
            tmp_path / f"g-{index}.bin",
            errors,
            governor=governor,
            max_connections=2,
            min_segment_size=128 * 1024,
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.join(30)
    elapsed = time.monotonic() - started

    assert not errors, errors
    # 1 MiB total with a 256 KiB burst bucket and 256 KiB/s afterwards.
    assert elapsed >= 1.4, elapsed
    for index in range(2):
        assert (tmp_path / f"g-{index}.bin").read_bytes() == data


def test_per_host_speed_limit_applies(server, tmp_path):
    data = make_content(1024 * 1024, seed=122)
    server.add("/slow.bin", Route(content=data, etag='"slow"'))
    policies = HostPolicies(default_speed_limit=512 * 1024)
    assert policies.resolve("127.0.0.1").speed_limit == 512 * 1024
    governor = Governor(policies)

    started = time.monotonic()
    run_download(
        server.url("/slow.bin"),
        tmp_path / "slow.bin",
        governor=governor,
        max_connections=4,
        min_segment_size=256 * 1024,
    )
    elapsed = time.monotonic() - started

    assert elapsed >= 0.9, elapsed
    assert (tmp_path / "slow.bin").read_bytes() == data


def test_cli_accepts_host_policy_flags(server, tmp_path):
    from personal_download_manager.cli import main

    data = make_content(256 * 1024, seed=123)
    server.add("/cli-pol.bin", Route(content=data, etag='"cli-pol"'))
    output = tmp_path / "cli-pol.bin"
    code = main(
        [
            "download",
            server.url("/cli-pol.bin"),
            "-o",
            str(output),
            "-n",
            "4",
            "--max-host-connections",
            "2",
            "--global-limit",
            "10M",
            "--no-progress",
            "--retries",
            "1",
            "--retry-delay",
            "0.01",
        ]
    )
    assert code == 0
    assert output.read_bytes() == data
