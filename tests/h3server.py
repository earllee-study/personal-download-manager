"""A minimal HTTP/3 (QUIC + TLS) test server built on aioquic.

Like ``tests/h2server.py`` this exists to prove the transport behaviour the
engine relies on: real QUIC handshakes with ALPN ``h3``, several concurrent
request streams on one connection, ranged responses and per-stream throttling
so benchmarks can show multiplexing rather than raw loopback throughput.

Only the subset the downloader uses is implemented: GET/HEAD, one ``Range``
header, fixed routes, optional response latency and per-stream rate limiting.
The certificate is the test-only self-signed one in ``tests/certs``.

Public API mirrors ``TestServer`` / ``H2Server``::

    server = H3Server(latency=0.02, throttle_bps=2 * 1024 * 1024).start()
    server.add("/f.bin", data)
    server.url("/f.bin")
    server.requests, server.connection_count, server.max_streams_per_connection

``aioquic`` is an optional dependency; tests that need this module import it
inside the test body and skip when it is missing.
"""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ProtocolNegotiated

CERTS = Path(__file__).resolve().parent / "certs"
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 64 * 1024


def parse_range(value: str | None, total: int):
    """Return ``(start, end)`` inclusive, or ``None`` for a full-body reply."""
    if not value or total <= 0:
        return None
    match = RANGE_RE.match(value.strip())
    if not match:
        return None
    start_s, end_s = match.groups()
    if not start_s and not end_s:
        return None
    if not start_s:
        length = int(end_s)
        if length <= 0:
            return None
        start, end = max(0, total - length), total - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    if start > end or start >= total:
        return None
    return start, min(end, total - 1)


@dataclass
class H3Request:
    method: str
    path: str
    range: str | None
    user_agent: str | None
    cookie: str | None
    headers: dict[str, str]


class _H3ServerProtocol(QuicConnectionProtocol):
    def __init__(self, quic, stream_handler=None, *, server: "H3Server") -> None:
        super().__init__(quic, stream_handler=stream_handler)
        self._http = H3Connection(quic)
        self._server = server
        self._active: set[int] = set()
        self._peak = 0
        server._connection_opened(self)

    def quic_event_received(self, event) -> None:
        if isinstance(event, ProtocolNegotiated):
            self._server._note_alpn(event.alpn_protocol)
            return
        for http_event in self._http.handle_event(event):
            if isinstance(http_event, HeadersReceived):
                asyncio.ensure_future(
                    self._respond(http_event.stream_id, http_event.headers)
                )
            elif isinstance(http_event, DataReceived):
                # The downloader sends bodyless requests; ignore any body.
                pass

    async def _respond(self, stream_id: int, headers) -> None:
        server = self._server
        header_map = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in headers
            if not name.startswith(b":")
        }
        method = dict(headers).get(b":method", b"GET").decode("latin-1")
        path = dict(headers).get(b":path", b"/").decode("latin-1")
        request = H3Request(
            method=method,
            path=path,
            range=header_map.get("range"),
            user_agent=header_map.get("user-agent"),
            cookie=header_map.get("cookie"),
            headers=header_map,
        )
        with server._lock:
            server.requests.append(request)
            self._active.add(stream_id)
            self._peak = max(self._peak, len(self._active))
            server.max_streams_per_connection = max(
                server.max_streams_per_connection, self._peak
            )
            server.peak_streams = max(server.peak_streams, len(self._active))
        try:
            if server.latency > 0:
                await asyncio.sleep(server.latency)

            content = server.routes.get(path.split("?", 1)[0])
            if content is None:
                self._send(stream_id, 404, [], b"")
                return

            total = len(content)
            parsed = parse_range(request.range, total)
            if parsed is None and request.range:
                start, end = 0, total - 1
                status = 200
                content_range = None
            elif parsed is None:
                start, end = 0, total - 1
                status = 200
                content_range = None
            else:
                start, end = parsed
                status = 206
                content_range = f"bytes {start}-{end}/{total}"
            body = content[start : end + 1]

            response_headers = [
                (b":status", str(status).encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"accept-ranges", b"bytes"),
                (b"content-type", b"application/octet-stream"),
                (b"etag", b'"h3-test"'),
            ]
            if content_range is not None:
                response_headers.append(
                    (b"content-range", content_range.encode("ascii"))
                )
            if method == "HEAD":
                self._http.send_headers(stream_id, response_headers, end_stream=True)
                self.transmit()
                return

            self._http.send_headers(stream_id, response_headers, end_stream=not body)
            self.transmit()
            if not body:
                return
            throttle = server.throttle_bps
            for offset in range(0, len(body), CHUNK):
                piece = body[offset : offset + CHUNK]
                last = offset + len(piece) >= len(body)
                self._http.send_data(stream_id, piece, end_stream=last)
                self.transmit()
                if throttle and not last:
                    await asyncio.sleep(len(piece) / throttle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced to the test through ``errors``
            with server._lock:
                server.errors.append(repr(exc))
        finally:
            with server._lock:
                self._active.discard(stream_id)

    def _send(self, stream_id: int, status: int, headers, body: bytes) -> None:
        block = [(b":status", str(status).encode("ascii"))]
        block.extend(headers)
        self._http.send_headers(stream_id, block, end_stream=not body)
        if body:
            self._http.send_data(stream_id, body, end_stream=True)
        self.transmit()


class H3Server:
    def __init__(
        self,
        *,
        latency: float = 0.0,
        throttle_bps: int | None = None,
        certfile: Path | str | None = None,
        keyfile: Path | str | None = None,
    ) -> None:
        self.routes: dict[str, bytes] = {}
        self.latency = float(latency)
        self.throttle_bps = int(throttle_bps) if throttle_bps else None
        self.certfile = Path(certfile or CERTS / "localhost.crt")
        self.keyfile = Path(keyfile or CERTS / "localhost.key")
        self.requests: list[H3Request] = []
        self.connection_count = 0
        self.peak_streams = 0
        self.max_streams_per_connection = 0
        self.alpn_protocols: set[str] = set()
        self.errors: list[str] = []
        self.lock = threading.Lock()
        self._lock = self.lock
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._server = None

    # -- lifecycle ------------------------------------------------------
    def add(self, path: str, content: bytes) -> None:
        self.routes[path] = content

    def start(self) -> "H3Server":
        self._thread = threading.Thread(
            target=self._run, name="h3-server", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("HTTP/3 test server did not start")
        if not self.port:
            raise RuntimeError(
                "HTTP/3 test server failed: " + "; ".join(self.errors)
            )
        return self

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and self._stop_event is not None:
            try:
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def url_for(self, path: str) -> str:
        return f"https://127.0.0.1:{self.port}{path}"

    # -- loop thread ----------------------------------------------------
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            with self._lock:
                self.errors.append(repr(exc))
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _main(self) -> None:
        configuration = QuicConfiguration(
            is_client=False, alpn_protocols=H3_ALPN
        )
        configuration.load_cert_chain(str(self.certfile), str(self.keyfile))

        def factory(quic, stream_handler=None):
            return _H3ServerProtocol(
                quic, stream_handler=stream_handler, server=self
            )

        server = await serve(
            "127.0.0.1", 0, configuration=configuration, create_protocol=factory
        )
        self._server = server
        transport = server._transport
        self.port = transport.get_extra_info("sockname")[1]
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            await self._stop_event.wait()
        finally:
            server.close()
            await asyncio.sleep(0)

    # -- accounting (called from the loop thread) ----------------------
    def _connection_opened(self, protocol: _H3ServerProtocol) -> None:
        with self._lock:
            self.connection_count += 1

    def _note_alpn(self, alpn: str | None) -> None:
        with self._lock:
            if alpn:
                self.alpn_protocols.add(str(alpn))
