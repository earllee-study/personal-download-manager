"""HTTP/3 (QUIC) transport: ALPN, one connection with many streams, ranges."""

from __future__ import annotations

import pytest

from personal_download_manager.client import make_client
from personal_download_manager.downloader import Downloader
from personal_download_manager.errors import FatalError
from personal_download_manager.models import DownloadConfig
from personal_download_manager.probe import probe
from server import make_content

pytest.importorskip("aioquic")

from h3server import H3Server  # noqa: E402
from udp_proxy import LossyUdpProxy  # noqa: E402


@pytest.fixture
def h3_server():
    server = H3Server(latency=0.1, throttle_bps=1024 * 1024).start()
    try:
        yield server
    finally:
        server.stop()


def _config(server, output, **overrides) -> DownloadConfig:
    defaults = dict(
        url=server.url_for("/h3.bin"),
        output=output,
        http3=True,
        verify=False,
        max_connections=8,
        min_segment_size=64 * 1024,
        min_split_size=64 * 1024,
        show_progress=False,
        quiet=True,
        timeout=20.0,
        connect_timeout=10.0,
        read_timeout=20.0,
    )
    defaults.update(overrides)
    return DownloadConfig(**defaults)


def test_http3_download_is_byte_exact_on_one_connection(h3_server, tmp_path):
    data = make_content(1024 * 1024, seed=401)
    h3_server.add("/h3.bin", data)
    output = tmp_path / "h3.bin"

    result = Downloader(_config(h3_server, output)).run()

    assert output.read_bytes() == data
    assert result.range_supported is True
    assert h3_server.alpn_protocols == {"h3"}
    # One connection for the probe, one for the transfer (all segments share
    # it); no per-worker connections.
    assert h3_server.connection_count == 2
    assert h3_server.max_streams_per_connection >= 4
    assert h3_server.errors == []
    ranges = [request.range for request in h3_server.requests if request.range]
    assert any(value.startswith("bytes=") for value in ranges)


def test_http3_probe_reports_range_metadata(h3_server):
    data = make_content(256 * 1024, seed=402)
    h3_server.add("/probe.bin", data)
    config = _config(h3_server, None, url=h3_server.url_for("/probe.bin"), http2=False)

    result = probe(config)

    assert result.status == 206
    assert result.ranges is True
    assert result.total_size == len(data)
    assert result.etag == '"h3-test"'
    assert result.http_version == "HTTP/3"


def test_http3_sends_site_headers(h3_server):
    h3_server.add("/hdr.bin", b"x" * 4096)
    config = _config(
        h3_server,
        None,
        url=h3_server.url_for("/hdr.bin"),
        user_agent="H3Bot/1.0",
        cookie="sid=42",
        referer="https://example.com/",
    )
    with make_client(config, max_connections=2) as client:
        response = client.get(
            h3_server.url_for("/hdr.bin"), headers={"Range": "bytes=0-15"}
        )
        assert response.status_code == 206
        assert response.content == b"x" * 16
        assert response.http_version == "HTTP/3"

    request = h3_server.requests[-1]
    assert request.user_agent == "H3Bot/1.0"
    assert request.cookie == "sid=42"
    assert request.headers.get("referer") == "https://example.com/"
    assert h3_server.errors == []


def test_http3_streams_hls_segments_on_one_connection(h3_server, tmp_path):
    from personal_download_manager.hls import download_hls

    segment_a = make_content(64 * 1024, seed=405)
    segment_b = make_content(64 * 1024, seed=406)
    h3_server.add("/vod/a.ts", segment_a)
    h3_server.add("/vod/b.ts", segment_b)
    h3_server.add(
        "/vod/media.m3u8",
        b"#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n",
    )
    output = tmp_path / "vod.ts"
    config = _config(
        h3_server,
        output,
        url=h3_server.url_for("/vod/media.m3u8"),
        max_connections=2,
    )
    download_hls(config)
    assert output.read_bytes() == segment_a + segment_b
    assert h3_server.alpn_protocols == {"h3"}
    # HLS has no separate probe: playlist and both segments share one connection.
    assert h3_server.connection_count == 1
    assert h3_server.max_streams_per_connection >= 2
    assert h3_server.errors == []


def test_http3_survives_packet_loss(h3_server, tmp_path):
    data = make_content(512 * 1024, seed=403)
    h3_server.add("/loss.bin", data)
    proxy = LossyUdpProxy(h3_server.port, loss=0.02, seed=11).start()
    try:
        config = _config(
            h3_server,
            tmp_path / "loss.bin",
            url=proxy.url_for("/loss.bin"),
            max_retries=5,
            retry_backoff=0.1,
        )
        Downloader(config).run()
        assert (tmp_path / "loss.bin").read_bytes() == data
        assert proxy.dropped > 0, "the proxy did not drop any datagrams"
        assert proxy.forwarded > 0
    finally:
        proxy.stop()


def test_http3_rejects_proxy(h3_server):
    config = _config(
        h3_server,
        None,
        url=h3_server.url_for("/never.bin"),
        proxy="socks5://127.0.0.1:1080",
    )
    with pytest.raises(FatalError, match="proxy"):
        make_client(config)


def test_http3_rejects_plain_http_url():
    config = DownloadConfig(
        url="http://127.0.0.1:9/x", output=None, http3=True, verify=False
    )
    client = make_client(config)
    try:
        with pytest.raises(Exception) as excinfo:
            client.get("http://127.0.0.1:9/x")
        assert "https" in str(excinfo.value).lower()
    finally:
        client.close()


def test_cli_download_over_http3(h3_server, tmp_path):
    from personal_download_manager.cli import main

    data = make_content(128 * 1024, seed=404)
    h3_server.add("/cli.bin", data)
    output = tmp_path / "cli.bin"
    code = main(
        [
            "download",
            h3_server.url_for("/cli.bin"),
            "-o",
            str(output),
            "--http3",
            "--insecure",
            "-n",
            "4",
            "--min-segment",
            "64K",
            "--min-split",
            "64K",
            "--retries",
            "1",
            "--retry-delay",
            "0.01",
            "--no-progress",
        ]
    )
    assert code == 0
    assert output.read_bytes() == data


def test_cli_rejects_conflicting_protocol_flags(tmp_path, capsys):
    from personal_download_manager.cli import main

    output = tmp_path / "x.bin"
    code = main(
        [
            "download",
            "https://example.com/x.bin",
            "-o",
            str(output),
            "--http2",
            "--http3",
        ]
    )
    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err

    code = main(
        [
            "download",
            "https://example.com/x.bin",
            "-o",
            str(output),
            "--http3",
            "--proxy",
            "http://127.0.0.1:8080",
        ]
    )
    assert code == 2
    assert "proxy" in capsys.readouterr().err
