"""A minimal HTTP/2 (TLS + h2) test server.

The HTTP/1.1 server in ``server.py`` cannot show multiplexing: every range
request needs its own connection.  This server speaks real HTTP/2 over TLS so
tests and benchmarks can verify that one TCP/TLS connection carries several
concurrent streams.

Only the subset the downloader uses is implemented: GET with an optional
single ``Range`` (and ``If-Range`` ignored, like a static file server), fixed
content, per-request start latency and per-stream throttling.  The certificate
is a test-only self-signed one in ``tests/certs`` (clients verify nothing).

Public API mirrors ``TestServer``::

    server = H2Server(latency=0.02, throttle_bps=2 * 1024 * 1024).start()
    server.add("/f.bin", data)
    server.url("/f.bin")
    server.requests, server.connection_count, server.peak_streams
"""

from __future__ import annotations

import re
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import h2.config
import h2.connection
import h2.events

CERTS = Path(__file__).resolve().parent / "certs"
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(value: str | None, total: int):
    """Return (start, end) inclusive, or ``None`` for a full-body response."""
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
class _Stream:
    stream_id: int
    path: str
    status: int
    body: bytes
    content_range: str | None
    record: dict
    start_at: float
    pos: int = 0
    headers_sent: bool = False
    done: bool = False
    last_pump: float = 0.0
    tokens: float = 0.0


class H2Server:
    def __init__(
        self,
        *,
        latency: float = 0.0,
        throttle_bps: int | None = None,
        certfile: Path | str | None = None,
        keyfile: Path | str | None = None,
        alpn_protocols: list[str] | None = None,
        port: int = 0,
    ) -> None:
        self.routes: dict[str, bytes] = {}
        self.latency = float(latency)
        self.throttle_bps = int(throttle_bps) if throttle_bps else None
        self.certfile = Path(certfile or CERTS / "localhost.crt")
        self.keyfile = Path(keyfile or CERTS / "localhost.key")
        # The default server only speaks h2.  Offering http/1.1 as well is a
        # diagnostic mode: a non-h2 client is recorded and disconnected.
        self.configured_alpn = list(alpn_protocols or ["h2"])
        self.port_setting = int(port)
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.connection_count = 0
        self.peak_streams = 0
        self.max_streams_per_connection = 0
        self.alpn_protocols: set[str] = set()
        self.handshakes: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connections: list[threading.Thread] = []
        self._sockets: set[socket.socket] = set()
        self._context: ssl.SSLContext | None = None

    # -- lifecycle ------------------------------------------------------
    def add(self, path: str, content: bytes) -> None:
        self.routes[path] = content

    def start(self) -> "H2Server":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.certfile), str(self.keyfile))
        context.set_alpn_protocols(self.configured_alpn)
        self._context = context

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port_setting))
        listener.listen(64)
        listener.setblocking(False)
        self._listener = listener
        self._thread = threading.Thread(
            target=self._accept_loop, name="h2-accept", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self.lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        for thread in list(self._connections):
            thread.join(timeout=2)

    @property
    def port(self) -> int:
        assert self._listener is not None
        return self._listener.getsockname()[1]

    def url(self, path: str) -> str:
        return f"https://127.0.0.1:{self.port}{path}"

    # -- accept / connection loop --------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                readable, _, _ = select.select([listener], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                raw, _addr = listener.accept()
            except OSError:
                continue
            thread = threading.Thread(
                target=self._serve, args=(raw,), name="h2-conn", daemon=True
            )
            self._connections.append(thread)
            thread.start()

    def _serve(self, raw: socket.socket) -> None:
        context = self._context
        assert context is not None
        try:
            sock = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        protocol = sock.selected_alpn_protocol()
        with self.lock:
            self.alpn_protocols.add(protocol or "")
            self.connection_count += 1
            connection_id = self.connection_count
            self.handshakes.append(
                {
                    "time": time.time(),
                    "connection": connection_id,
                    "alpn": protocol or "",
                }
            )
            self._sockets.add(sock)
        if protocol != "h2":
            # Nothing else is promised by this server; close politely.
            self._close(sock)
            return

        config = h2.config.H2Configuration(
            client_side=False, header_encoding="utf-8"
        )
        conn = h2.connection.H2Connection(config=config)
        streams: dict[int, _Stream] = {}
        conn_peak = 0
        try:
            conn.initiate_connection()
            sock.sendall(conn.data_to_send())
            while not self._stop.is_set():
                now = time.monotonic()
                active = 0
                for stream in list(streams.values()):
                    if stream.done:
                        continue
                    active += 1
                    if not stream.headers_sent and now >= stream.start_at:
                        self._send_headers(conn, stream)
                    if stream.headers_sent:
                        self._pump(conn, stream, now)
                conn_peak = max(conn_peak, active)
                with self.lock:
                    self.peak_streams = max(self.peak_streams, active)

                pending = conn.data_to_send()
                if pending:
                    sock.sendall(pending)

                timeout = self._wait_timeout(streams, now)
                try:
                    readable, _, _ = select.select([sock], [], [], timeout)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
                data = sock.recv(65536)
                if not data:
                    break
                events = conn.receive_data(data)
                terminated = False
                for event in events:
                    if isinstance(event, h2.events.RequestReceived):
                        self._on_request(
                            event, streams, conn, connection_id, protocol or ""
                        )
                    elif isinstance(event, h2.events.PingReceived):
                        conn.ping_ack(event.ping_data)
                    elif isinstance(event, h2.events.WindowUpdated):
                        pass  # the pump loop will see the new window
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        terminated = True
                pending = conn.data_to_send()
                if pending:
                    sock.sendall(pending)
                if terminated:
                    break
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # The client finished and closed the TLS connection; not an error.
            pass
        except (OSError, ssl.SSLError, h2.exceptions.H2Error) as exc:
            with self.lock:
                self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                self.max_streams_per_connection = max(
                    self.max_streams_per_connection, conn_peak
                )
            self._close(sock)

    def _close(self, sock: socket.socket) -> None:
        with self.lock:
            self._sockets.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    # -- request handling ----------------------------------------------
    def _on_request(
        self,
        event: h2.events.RequestReceived,
        streams: dict[int, _Stream],
        conn,
        connection_id: int,
        alpn: str,
    ) -> None:
        headers = dict(event.headers)
        path = str(headers.get(":path", "")).split("?")[0]
        range_header = headers.get("range")
        content = self.routes.get(path)
        if content is None:
            status, body, content_range = 404, b"", None
        else:
            parsed = parse_range(range_header, len(content))
            if parsed is None:
                status, body, content_range = 200, content, None
            else:
                start, end = parsed
                status = 206
                body = content[start : end + 1]
                content_range = f"bytes {start}-{end}/{len(content)}"
        record = {
            "method": str(headers.get(":method", "GET")),
            "path": path,
            "range": range_header,
            "status": status,
            "bytes": len(body),
            "time": time.time(),
            "end": None,
            "stream": event.stream_id,
            "connection": connection_id,
            "alpn": alpn,
        }
        with self.lock:
            self.requests.append(record)
        now = time.monotonic()
        stream = _Stream(
            stream_id=event.stream_id,
            path=path,
            status=status,
            body=body,
            content_range=content_range,
            record=record,
            start_at=now + self.latency,
            last_pump=now,
        )
        streams[event.stream_id] = stream
        if not body:
            self._send_headers(conn, stream)
            conn.end_stream(event.stream_id)
            with self.lock:
                record["end"] = time.time()
            stream.done = True

    def _send_headers(self, conn, stream: _Stream) -> None:
        headers = [
            (":status", str(stream.status)),
            ("content-type", "application/octet-stream"),
            ("content-length", str(len(stream.body))),
            ("accept-ranges", "bytes"),
        ]
        if stream.content_range:
            headers.append(("content-range", stream.content_range))
        conn.send_headers(stream.stream_id, headers)
        stream.headers_sent = True
        stream.last_pump = time.monotonic()

    def _pump(self, conn, stream: _Stream, now: float) -> None:
        """Send as much as the flow-control window and throttle allow."""
        if stream.done:
            return
        if self.throttle_bps:
            elapsed = max(0.0, now - stream.last_pump)
            stream.last_pump = now
            burst = max(16 * 1024, self.throttle_bps // 4)
            stream.tokens = min(burst, stream.tokens + elapsed * self.throttle_bps)

        # One megabyte per call keeps the loop responsive without making the
        # Python server the bottleneck for unthrottled benchmarks.
        budget = 1024 * 1024
        while budget > 0 and not stream.done:
            try:
                window = conn.local_flow_control_window(stream.stream_id)
            except h2.exceptions.StreamClosedError:
                stream.done = True
                return
            remaining = len(stream.body) - stream.pos
            if window <= 0 or remaining <= 0:
                return
            allowed = int(stream.tokens) if self.throttle_bps else window
            if allowed <= 0:
                return
            # Never exceed the peer's maximum frame size (16384 by default).
            size = min(window, allowed, remaining, conn.max_outbound_frame_size, budget)
            chunk = stream.body[stream.pos : stream.pos + size]
            stream.pos += size
            if self.throttle_bps:
                stream.tokens -= size
            budget -= size
            finished = stream.pos >= len(stream.body)
            conn.send_data(stream.stream_id, chunk, end_stream=finished)
            if finished:
                stream.done = True
                with self.lock:
                    stream.record["end"] = time.time()

    def _wait_timeout(self, streams: dict[int, _Stream], now: float) -> float:
        waits = [
            0.0
            for stream in streams.values()
            if not stream.done
            and stream.headers_sent
            and stream.pos < len(stream.body)
        ]
        starts = [
            max(0.0, stream.start_at - now)
            for stream in streams.values()
            if not stream.done and not stream.headers_sent
        ]
        if waits:
            # Unthrottled streams only wait for a flow-control window update;
            # poll instead of sleeping so the server is not the bottleneck.
            return 0.0 if self.throttle_bps is None else 0.001
        if starts:
            return min(0.05, min(starts))
        return 0.2
