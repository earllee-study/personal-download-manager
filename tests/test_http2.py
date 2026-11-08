"""HTTP/2 multiplexing against a real TLS + h2 test server.

Until now ``--http2`` was only verified by hand against a public host.  These
tests pin down the property that matters: one TCP/TLS connection carries all
segments concurrently, while the HTTP/1.1 path still opens one connection per
worker.  The server certificate in ``tests/certs`` is test-only and the client
runs with verification disabled.
"""

from __future__ import annotations

from personal_download_manager import DownloadConfig, Downloader
from server import Route, make_content
from h2server import H2Server


def _run(server: H2Server, tmp_path, name: str, **overrides):
    data = overrides.pop("data")
    server.add(f"/{name}", data)
    output = tmp_path / name
    defaults = dict(
        url=server.url(f"/{name}"),
        output=output,
        max_connections=6,
        min_segment_size=256 * 1024,
        min_split_size=256 * 1024,
        max_retries=0,
        http2=True,
        verify=False,
        show_progress=False,
        quiet=True,
    )
    defaults.update(overrides)
    result = Downloader(DownloadConfig(**defaults)).run()
    return result, output, data


def test_http2_negotiates_and_downloads_byte_exact(tmp_path):
    # Throttle each stream so the segments genuinely overlap; without it a
    # loopback transfer can finish before all workers have started.
    server = H2Server(throttle_bps=1024 * 1024).start()
    try:
        data = make_content(2 * 1024 * 1024, seed=951)
        result, output, data = _run(server, tmp_path, "h2.bin", data=data)
        assert output.read_bytes() == data
        assert result.range_supported is True
        assert server.alpn_protocols == {"h2"}
        assert not server.errors, server.errors
        # One connection for the probe + one shared connection for every
        # segment.  The probe client is separate by design.
        assert server.connection_count == 2, server.connection_count
        assert server.max_streams_per_connection >= 4
    finally:
        server.stop()


def test_h2_server_records_connection_stream_and_alpn_metadata(tmp_path):
    """These fields let tests attribute requests to individual runs."""
    server = H2Server(throttle_bps=1024 * 1024).start()
    try:
        data = make_content(1024 * 1024, seed=955)
        _result, output, data = _run(server, tmp_path, "meta.bin", data=data)
        assert output.read_bytes() == data
        transfers = [
            record
            for record in server.requests
            if record["range"] and record["range"] != "bytes=0-0"
        ]
        assert transfers
        for record in transfers:
            assert isinstance(record["connection"], int)
            assert record["alpn"] == "h2"
            assert isinstance(record["end"], float)
            assert record["end"] >= record["time"]
        assert server.handshakes
        assert {item["alpn"] for item in server.handshakes} == {"h2"}
        assert {item["connection"] for item in server.handshakes} == {
            record["connection"] for record in server.requests
        }
    finally:
        server.stop()


def test_http1_tls_baseline_needs_a_connection_per_worker(tmp_path):
    from server import TestServer

    server = TestServer(tls=True).start()
    try:
        data = make_content(2 * 1024 * 1024, seed=952)
        server.add("/h1.bin", Route(content=data, etag='"h1"'))
        output = tmp_path / "h1.bin"
        Downloader(
            DownloadConfig(
                url=server.url("/h1.bin"),
                output=output,
                max_connections=4,
                min_segment_size=256 * 1024,
                min_split_size=256 * 1024,
                max_retries=0,
                http2=False,
                verify=False,
                show_progress=False,
                quiet=True,
            )
        ).run()
        assert output.read_bytes() == data
        # HTTP/1.1 cannot multiplex: concurrent ranges arrive on distinct
        # client ports, one per worker connection.
        ports = {
            record["client_port"]
            for record in server.requests
            if record["range"] and record["range"] != "bytes=0-0"
        }
        assert len(ports) >= 4, ports
    finally:
        server.stop()


def test_http2_flag_degrades_to_http1_for_cleartext_servers(server, tmp_path):
    data = make_content(1024 * 1024, seed=953)
    server.add("/plain.bin", Route(content=data, etag='"plain"'))
    output = tmp_path / "plain.bin"
    Downloader(
        DownloadConfig(
            url=server.url("/plain.bin"),
            output=output,
            max_connections=4,
            min_segment_size=256 * 1024,
            http2=True,  # http:// cannot use h2; httpx keeps HTTP/1.1
            show_progress=False,
            quiet=True,
        )
    ).run()
    assert output.read_bytes() == data


def test_h2_server_shares_the_connection_under_load(tmp_path):
    server = H2Server(throttle_bps=2 * 1024 * 1024).start()
    try:
        data = make_content(4 * 1024 * 1024, seed=954)
        result, output, data = _run(
            server,
            tmp_path,
            "load.bin",
            data=data,
            max_connections=8,
            min_segment_size=256 * 1024,
            min_split_size=256 * 1024,
        )
        assert output.read_bytes() == data
        assert server.max_streams_per_connection >= 6
        assert server.connection_count == 2
        assert not server.errors, server.errors
    finally:
        server.stop()
