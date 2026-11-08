"""A controllable HTTP/1.1 test server.

It implements exactly the behaviours the engine must survive:
  * single-range 206 responses, suffix ranges, 416
  * If-Range validator checks
  * servers that ignore Range (200 full body)
  * chunked transfer encoding
  * per-connection throttling (to make segmentation observable)
  * dropped connections, 503 storms, auth, redirects, Content-Disposition

Only the standard library is used, so tests stay dependency-light.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def make_content(size: int, seed: int = 0) -> bytes:
    """Fast deterministic pseudo-random bytes (no os.urandom in tests)."""
    if size <= 0:
        return b""
    block = hashlib.sha256(f"pdm-test-{seed}".encode()).digest()
    pattern = (block * ((64 * 1024) // len(block) + 1))[: 64 * 1024]
    return (pattern * (size // len(pattern) + 1))[:size]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Route:
    content: bytes = b""
    etag: str | None = None
    last_modified: str | None = "Wed, 21 Oct 2015 07:28:00 GMT"
    content_type: str = "application/octet-stream"
    content_disposition: str | None = None
    content_encoding: str | None = None
    omit_content_range: bool = False  # omit Content-Range on non-probe 206s
    ignore_range: bool = False
    chunked: bool = False
    reject_range_requests: bool = False  # 405 to a GET carrying Range
    throttle_bps: int | None = None
    first_range_fast: bool = False
    chunk_size: int = 64 * 1024
    # Congestion simulation: more than this many simultaneous range responses
    # get 503 + Retry-After (the scenario AIMD exists for).
    max_parallel: int | None = None

    redirect_to: str | None = None
    require_auth: tuple[str, str] | None = None

    # Fault injection
    fail_times: int = 0          # 503 for every request (including probe)
    fail_range_times: int = 0    # 503 for non-probe Range requests
    drop_after: int | None = None
    drop_times: int = 1

    # Mutable counters (guarded by the owning server's lock)
    fail_times_left: int = field(default=0, init=False)
    fail_range_left: int = field(default=0, init=False)
    drop_left: int = field(default=0, init=False)
    range_requests: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.fail_times_left = self.fail_times
        self.fail_range_left = self.fail_range_times
        self.drop_left = self.drop_times if self.drop_after is not None else 0


class TestServer:
    def __init__(self, *, tls: bool = False, latency: float = 0.0) -> None:
        self.tls = tls
        self.latency = float(latency)
        self.routes: dict[str, Route] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.inflight = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def try_enter(self, route: Route) -> bool:
        """Admit one tracked range response, or reject it (503)."""
        with self.lock:
            if route.max_parallel is not None and self.inflight >= route.max_parallel:
                return False
            self.inflight += 1
            return True

    def leave(self) -> None:
        with self.lock:
            self.inflight = max(0, self.inflight - 1)

    # -- lifecycle ------------------------------------------------------
    def add(self, path: str, route: Route | None = None, **kwargs) -> Route:
        if route is None:
            route = Route(**kwargs)
        self.routes[path] = route
        return route

    def start(self) -> "TestServer":
        app = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            app_ref = app

        self.httpd = Server(("127.0.0.1", 0), Handler)
        if self.tls:
            import ssl

            certs = Path(__file__).resolve().parent / "certs"
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                str(certs / "localhost.crt"), str(certs / "localhost.key")
            )
            context.set_alpn_protocols(["http/1.1"])
            self.httpd.socket = context.wrap_socket(
                self.httpd.socket, server_side=True
            )
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="test-http", daemon=True
        )
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=3)
            self.thread = None

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    def url(self, path: str) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.port}{path}"

    # -- bookkeeping ----------------------------------------------------
    def record(self, item: dict) -> None:
        with self.lock:
            self.requests.append(item)

    def take_fail(self, route: Route) -> bool:
        with self.lock:
            if route.fail_times_left > 0:
                route.fail_times_left -= 1
                return True
            return False

    def take_range_fail(self, route: Route) -> bool:
        with self.lock:
            if route.fail_range_left > 0:
                route.fail_range_left -= 1
                return True
            return False

    def take_drop(self, route: Route) -> bool:
        with self.lock:
            if route.drop_left > 0:
                route.drop_left -= 1
                return True
            return False

    def range_index(self, route: Route) -> int:
        with self.lock:
            index = route.range_requests
            route.range_requests += 1
            return index


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pdm-test"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Cancellation tests deliberately close sockets mid-response.
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head=True)

    # ------------------------------------------------------------------
    def _handle(self, head: bool) -> None:
        app: TestServer = self.server.app_ref  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        route = app.routes.get(path)
        range_header = self.headers.get("Range")
        is_probe = range_header == "bytes=0-0"
        record = {
            "method": self.command,
            "path": path,
            "raw": self.path,  # includes the query string (benchmark run markers)
            "range": range_header,
            "if_range": self.headers.get("If-Range"),
            "status": None,
            "bytes": 0,
            "auth": self.headers.get("Authorization"),
            "ua": self.headers.get("User-Agent"),
            "cookie": self.headers.get("Cookie"),
            "referer": self.headers.get("Referer"),
            "client_port": self.client_address[1] if self.client_address else None,
            "time": time.time(),
        }
        app.record(record)

        if app.latency:
            # Simulated request latency (RTT-like) for HTTP/2 comparisons.
            time.sleep(app.latency)

        if route is None:
            record["status"] = 404
            self._empty(404)
            return

        if route.redirect_to is not None:
            record["status"] = 302
            self.send_response(302)
            self.send_header("Location", route.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if route.require_auth is not None:
            expected = "Basic " + base64.b64encode(
                f"{route.require_auth[0]}:{route.require_auth[1]}".encode()
            ).decode()
            if self.headers.get("Authorization") != expected:
                record["status"] = 401
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        if app.take_fail(route):
            record["status"] = 503
            self._empty(503, {"Retry-After": "0"})
            return

        if range_header and route.reject_range_requests:
            record["status"] = 405
            self._empty(405)
            return

        if range_header and not is_probe and app.take_range_fail(route):
            record["status"] = 503
            self._empty(503, {"Retry-After": "0"})
            return

        total = len(route.content)
        use_range = bool(range_header) and not route.ignore_range
        if use_range:
            if_range = self.headers.get("If-Range")
            if if_range is not None:
                validators = {v for v in (route.etag, route.last_modified) if v}
                if if_range.strip() not in validators:
                    use_range = False  # validator changed -> full response
        parsed = None
        if use_range:
            parsed = self._parse_range(range_header or "", total)
            if parsed == (None, None):
                record["status"] = 416
                headers = {"Content-Range": f"bytes */{total}"}
                self._empty(416, headers)
                return

        if parsed is not None:
            start, end = parsed
            body = route.content[start : end + 1]
            status = 206
            extra = {
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Length": str(len(body)),
            }
        else:
            start, end = 0, max(0, total - 1)
            body = route.content
            status = 200
            extra = {"Content-Length": str(total)}

        if route.omit_content_range and status == 206 and not is_probe:
            extra.pop("Content-Range", None)

        # Congestion simulation: reject extra concurrent range responses.
        tracked = bool(range_header) and not is_probe
        if route.max_parallel is not None and tracked:
            if not app.try_enter(route):
                record["status"] = 503
                record["end"] = time.time()
                self._empty(503, {"Retry-After": "1"})
                return
        try:
            record["status"] = status
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", route.content_type)
            if route.content_encoding:
                self.send_header("Content-Encoding", route.content_encoding)
            if route.etag:
                self.send_header("ETag", route.etag)
            if route.last_modified:
                self.send_header("Last-Modified", route.last_modified)
            if route.content_disposition:
                self.send_header("Content-Disposition", route.content_disposition)
            for name, value in extra.items():
                if not (route.chunked and name == "Content-Length"):
                    self.send_header(name, value)
            if route.chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            if head:
                return

            throttle = route.throttle_bps
            if (
                route.first_range_fast
                and range_header
                and not is_probe
                and app.range_index(route) == 0
            ):
                throttle = 64 * 1024 * 1024

            drop_at = None
            if range_header and not is_probe and app.take_drop(route):
                drop_at = route.drop_after

            sent = self._send_body(
                body,
                chunked=route.chunked,
                chunk_size=route.chunk_size,
                throttle_bps=throttle,
                drop_after=drop_at,
            )
            record["bytes"] = sent
            # Benchmarks use the end time to measure real concurrency per run.
            record["end"] = time.time()
        finally:
            if route.max_parallel is not None and tracked:
                app.leave()

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_range(value: str, total: int):
        match = RANGE_RE.match(value.strip())
        if not match:
            return None
        start_s, end_s = match.groups()
        if not start_s and not end_s:
            return None
        if total <= 0:
            return (None, None)
        if not start_s:
            length = int(end_s)
            if length <= 0:
                return (None, None)
            start = max(0, total - length)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        if start > end or start >= total:
            return (None, None)
        return start, min(end, total - 1)

    def _empty(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_body(
        self,
        body: bytes,
        *,
        chunked: bool,
        chunk_size: int,
        throttle_bps: int | None,
        drop_after: int | None,
    ) -> int:
        start_time = time.monotonic()
        sent = 0
        chunk_size = max(1024, chunk_size)
        try:
            for offset in range(0, len(body), chunk_size):
                chunk = body[offset : offset + chunk_size]
                if drop_after is not None and sent + len(chunk) >= drop_after:
                    keep = max(0, drop_after - sent)
                    if keep:
                        self._write_chunk(chunk[:keep], chunked)
                        sent += keep
                    self.wfile.flush()
                    self.close_connection = True
                    return sent
                self._write_chunk(chunk, chunked)
                sent += len(chunk)
                if throttle_bps:
                    expected = sent / throttle_bps
                    elapsed = time.monotonic() - start_time
                    if expected > elapsed:
                        time.sleep(min(expected - elapsed, 0.25))
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            # The client (typically the 0-0 probe) closed early.
            self.close_connection = True
        return sent

    def _write_chunk(self, chunk: bytes, chunked: bool) -> None:
        if chunked:
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
        else:
            self.wfile.write(chunk)
