"""Per-site credentials (User-Agent, Cookie, Basic/Digest auth).

Site rules are applied by the engine from the request's final host, so a rule
must be visible on the probe request *and* on every segment request.
"""

from __future__ import annotations

import base64

import pytest

from helpers import run_download
from personal_download_manager.gui.settings import AppSettings, governor_from_settings
from personal_download_manager.policy import Governor, HostPolicies, HostRule
from server import Route, make_content


def test_rule_roundtrip_carries_site_credentials():
    rule = HostRule(
        "*.example.com",
        connections=4,
        user_agent="SiteBot/1.0",
        cookie="sid=42",
        auth="user:pass",
        auth_type="digest",
    )
    restored = HostRule.from_dict(rule.to_dict())
    assert restored == rule
    assert restored.to_dict()["auth"] == "user:pass"


def test_resolve_exposes_credentials_only_for_matching_rules():
    policies = HostPolicies(
        [
            HostRule(
                "*.example.com",
                user_agent="SiteBot/1.0",
                cookie="sid=42",
                auth="user:pass",
            )
        ]
    )
    matched = policies.resolve("https://cdn.example.com/a.bin")
    assert matched.user_agent == "SiteBot/1.0"
    assert matched.cookie == "sid=42"
    assert matched.auth == ("user", "pass")
    assert matched.auth_type == "basic"

    other = policies.resolve("https://elsewhere.org/a.bin")
    assert other.user_agent is None
    assert other.cookie is None
    assert other.auth is None


def test_rule_validation_rejects_a_malformed_auth():
    with pytest.raises(ValueError):
        HostRule("x", auth="no-colon")
    with pytest.raises(ValueError):
        HostRule("x", auth="u:p", auth_type="ntlm")


def _records_for(server, path: str) -> list[dict]:
    return [item for item in server.requests if item["path"] == path]


def test_site_basic_auth_is_sent_on_every_request(server, tmp_path):
    data = make_content(1024 * 1024, seed=300)
    server.add(
        "/site-auth.bin",
        Route(content=data, etag='"sa"', require_auth=("alice", "secret")),
    )
    governor = Governor(
        HostPolicies([HostRule("127.0.0.1", auth="alice:secret")])
    )
    result = run_download(
        server.url("/site-auth.bin"),
        tmp_path / "site-auth.bin",
        governor=governor,
        max_connections=4,
        min_segment_size=256 * 1024,
        min_split_size=256 * 1024,
    )
    assert result.path.read_bytes() == data

    expected = "Basic " + base64.b64encode(b"alice:secret").decode()
    records = _records_for(server, "/site-auth.bin")
    assert records, "the server saw no requests"
    assert {record["auth"] for record in records} == {expected}


def test_site_user_agent_and_cookie_override_task_values(server, tmp_path):
    data = make_content(512 * 1024, seed=301)
    server.add("/site-ua.bin", Route(content=data, etag='"su"'))
    governor = Governor(
        HostPolicies(
            [
                HostRule(
                    "127.0.0.1",
                    user_agent="SiteBot/2.0",
                    cookie="site=yes",
                )
            ]
        )
    )
    run_download(
        server.url("/site-ua.bin"),
        tmp_path / "site-ua.bin",
        governor=governor,
        max_connections=4,
        min_segment_size=128 * 1024,
        user_agent="TaskAgent/1.0",
        cookie="task=no",
    )
    records = _records_for(server, "/site-ua.bin")
    assert records
    assert {record["ua"] for record in records} == {"SiteBot/2.0"}
    assert {record["cookie"] for record in records} == {"site=yes"}


def test_rule_leaves_unset_fields_to_the_task(server, tmp_path):
    data = make_content(256 * 1024, seed=302)
    server.add("/site-mixed.bin", Route(content=data, etag='"sm"'))
    governor = Governor(
        HostPolicies([HostRule("127.0.0.1", cookie="site=yes")])
    )
    run_download(
        server.url("/site-mixed.bin"),
        tmp_path / "site-mixed.bin",
        governor=governor,
        max_connections=2,
        user_agent="TaskAgent/1.0",
        referer="https://ref.example/",
    )
    records = _records_for(server, "/site-mixed.bin")
    assert records
    # The rule only sets the cookie; UA and Referer stay task-level.
    assert {record["ua"] for record in records} == {"TaskAgent/1.0"}
    assert {record["cookie"] for record in records} == {"site=yes"}
    assert {record["referer"] for record in records} == {"https://ref.example/"}


def test_cli_policy_file_supplies_site_auth(server, tmp_path):
    import json

    from personal_download_manager.cli import main

    data = make_content(128 * 1024, seed=303)
    server.add(
        "/cli-site.bin",
        Route(content=data, require_auth=("eve", "pw")),
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "pattern": "127.0.0.1",
                        "auth": "eve:pw",
                        "user_agent": "PolicyBot/1.0",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "cli-site.bin"
    code = main(
        [
            "download",
            server.url("/cli-site.bin"),
            "-o",
            str(output),
            "-n",
            "2",
            "--policy",
            str(policy_path),
            "--no-progress",
            "--retries",
            "1",
            "--retry-delay",
            "0.01",
        ]
    )
    assert code == 0
    assert output.read_bytes() == data
    assert {record["ua"] for record in _records_for(server, "/cli-site.bin")} == {
        "PolicyBot/1.0"
    }


def test_gui_settings_build_site_rules():
    settings = AppSettings(
        host_rules=[
            {
                "pattern": "*.test",
                "user_agent": "SiteBot/3.0",
                "cookie": "a=1",
                "auth": "bob:pw",
            }
        ]
    )
    governor = governor_from_settings(settings)
    policy = governor.resolve("https://x.test/file")
    assert policy.user_agent == "SiteBot/3.0"
    assert policy.cookie == "a=1"
    assert policy.auth == ("bob", "pw")


def test_mirror_uses_its_own_site_credentials(server, tmp_path):
    """A mirror on another host gets that host's rule, not the task's values."""
    from server import TestServer

    data = make_content(2 * 1024 * 1024, seed=304)
    server.add("/mirror.bin", Route(content=data, etag='"mirror"'))
    second = TestServer().start()
    try:
        second.add(
            "/mirror.bin",
            Route(content=data, etag='"mirror"', require_auth=("m", "pw")),
        )
        mirror_url = f"http://localhost:{second.port}/mirror.bin"
        governor = Governor(
            HostPolicies(
                [HostRule("localhost", auth="m:pw", user_agent="MirrorBot/1.0")]
            )
        )
        run_download(
            server.url("/mirror.bin"),
            tmp_path / "mirror.bin",
            governor=governor,
            mirrors=[mirror_url],
            max_connections=4,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
        assert (tmp_path / "mirror.bin").read_bytes() == data

        mirror_records = [
            item for item in second.requests if item["path"] == "/mirror.bin"
        ]
        assert mirror_records
        assert all(item["auth"] for item in mirror_records)
        assert {item["ua"] for item in mirror_records} == {"MirrorBot/1.0"}

        primary_records = [
            item for item in server.requests if item["path"] == "/mirror.bin"
        ]
        assert primary_records
        assert {item["ua"] for item in primary_records} != {"MirrorBot/1.0"}
    finally:
        second.stop()


def test_hls_applies_site_credentials_to_playlist_and_segments(server, tmp_path):
    from personal_download_manager.hls import download_hls
    from personal_download_manager.models import DownloadConfig

    seg0 = make_content(128 * 1024, seed=305)
    seg1 = make_content(128 * 1024, seed=306)
    auth = ("hls", "pw")
    server.add("/sh/seg0.ts", Route(content=seg0, require_auth=auth))
    server.add("/sh/seg1.ts", Route(content=seg1, require_auth=auth))
    playlist = server.add("/sh/media.m3u8", Route(content=b"", require_auth=auth))
    playlist.content = (
        "#EXTM3U\n#EXTINF:1,\nseg0.ts\n#EXTINF:1,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()

    governor = Governor(
        HostPolicies([HostRule("127.0.0.1", auth="hls:pw", user_agent="HlsBot/1.0")])
    )
    output = tmp_path / "hls.ts"
    download_hls(
        DownloadConfig(
            url=server.url("/sh/media.m3u8"),
            output=output,
            max_connections=2,
            governor=governor,
            show_progress=False,
            quiet=True,
        )
    )
    assert output.read_bytes() == seg0 + seg1

    expected = "Basic " + base64.b64encode(b"hls:pw").decode()
    for path in ("/sh/media.m3u8", "/sh/seg0.ts", "/sh/seg1.ts"):
        records = [
            item
            for item in server.requests
            if item["path"] == path and item["status"] != 401
        ]
        assert records, path
        assert {item["auth"] for item in records} == {expected}
        assert {item["ua"] for item in records} == {"HlsBot/1.0"}
