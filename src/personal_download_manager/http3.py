"""HTTP/3 (QUIC) support, exposed as an httpx transport.

The rest of the engine speaks httpx.  Rather than fork the download path for a
second protocol, this module provides a ``BaseTransport`` that turns every
``GET``/``HEAD`` into an HTTP/3 request on a shared QUIC connection.  Probe,
redirects, cookies, Basic/Digest authentication, range verification and the
segment workers therefore keep working unchanged.

Layout:

* :class:`_OriginClient` owns one asyncio loop in a daemon thread and keeps a
  QUIC connection to one ``host:port`` alive (reconnecting after an idle
  timeout or a server close).
* :class:`_H3Protocol` multiplexes every request as a new bidirectional
  stream and routes ``HeadersReceived``/``DataReceived`` events back to the
  matching exchange.
* :class:`HTTP3Transport` is the synchronous httpx facade: requests from
  several worker threads can be in flight at once, and response bodies are
  handed over through a thread-safe queue as they arrive.

aioquic is an optional dependency.  Importing this module without it works;
constructing the transport raises :class:`~personal_download_manager.errors.FatalError`.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import ssl
import threading
from typing import Any

import httpx

from .errors import FatalError

# aioquic logs one INFO line per handshake/ALPN/close on the "quic" logger,
# which would drown the CLI's own output.  Keep warnings and errors only.
logging.getLogger("quic").setLevel(logging.WARNING)

try:  # pragma: no cover - exercised by the HTTP/3 tests when installed
    from aioquic.asyncio.client import connect as _quic_connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import ConnectionTerminated, StreamReset

    AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the environment
    AVAILABLE = False

# RFC 9114 H3_REQUEST_CANCELLED.
H3_REQUEST_CANCELLED = 0x010C

# Headers that belong to HTTP/1.x framing or are implied by HTTP/3 pseudo
# fields; sending them is either illegal or redundant.
_SKIP_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
}


class HTTP3Unavailable(FatalError):
    pass


def available() -> bool:
    """Whether aioquic is importable (``--http3`` needs it)."""
    return AVAILABLE


if AVAILABLE:

    class _Exchange:
        """One HTTP/3 stream: headers plus a blocking body queue."""

        def __init__(self, stream_id: int) -> None:
            self.stream_id = stream_id
            self.status: int | None = None
            self.headers: list[tuple[str, str]] = []
            self.error: httpx.HTTPError | None = None
            self.done = False
            self.headers_ready = asyncio.Event()
            self.chunks: queue.Queue[bytes | None] = queue.Queue()

        # -- called from the event loop ---------------------------------
        def headers_event(self, headers: list[tuple[bytes, bytes]], ended: bool) -> None:
            if self.status is None:
                for name, value in headers:
                    if name == b":status":
                        try:
                            self.status = int(value)
                        except ValueError:
                            self.fail(
                                httpx.RemoteProtocolError(
                                    f"invalid :status {value!r}"
                                )
                            )
                            return
                    elif not name.startswith(b":"):
                        self.headers.append(
                            (name.decode("latin-1"), value.decode("latin-1"))
                        )
                if self.status is None:
                    self.fail(httpx.RemoteProtocolError("HTTP/3 response without :status"))
                    return
                self.headers_ready.set()
            # A second HEADERS block is trailers; they are not displayed.
            if ended:
                self.finish()

        def data_event(self, data: bytes, ended: bool) -> None:
            if data:
                self.chunks.put(data)
            if ended:
                self.finish()

        def finish(self) -> None:
            if self.done:
                return
            self.done = True
            self.chunks.put(None)
            self.headers_ready.set()

        def fail(self, exc: httpx.HTTPError) -> None:
            if self.error is None:
                self.error = exc
            self.finish()

    class _H3Protocol(QuicConnectionProtocol):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._http = H3Connection(self._quic)
            self._exchanges: dict[int, _Exchange] = {}
            self.terminated = asyncio.Event()

        async def request(
            self,
            method: bytes,
            target: bytes,
            authority: bytes,
            headers: list[tuple[bytes, bytes]],
            timeout: float,
        ) -> _Exchange:
            stream_id = self._quic.get_next_available_stream_id()
            exchange = _Exchange(stream_id)
            self._exchanges[stream_id] = exchange
            block = [
                (b":method", method),
                (b":scheme", b"https"),
                (b":authority", authority),
                (b":path", target),
            ]
            block.extend(headers)
            try:
                self._http.send_headers(stream_id, block, end_stream=True)
                self.transmit()
                await asyncio.wait_for(exchange.headers_ready.wait(), timeout)
            except BaseException:
                self.discard(stream_id)
                raise
            return exchange

        # -- event loop side --------------------------------------------
        def quic_event_received(self, event: Any) -> None:
            if isinstance(event, ConnectionTerminated):
                reason = event.reason_phrase or "connection closed"
                for exchange in list(self._exchanges.values()):
                    exchange.fail(httpx.ReadError(f"HTTP/3 {reason}"))
                self._exchanges.clear()
                self.terminated.set()
                return
            if isinstance(event, StreamReset):
                exchange = self._exchanges.pop(event.stream_id, None)
                if exchange is not None:
                    exchange.fail(
                        httpx.ReadError(
                            f"HTTP/3 stream reset by peer ({event.error_code})"
                        )
                    )
                return
            try:
                http_events = self._http.handle_event(event)
            except Exception as exc:  # malformed peer data
                for exchange in list(self._exchanges.values()):
                    exchange.fail(httpx.RemoteProtocolError(f"HTTP/3 error: {exc}"))
                self._exchanges.clear()
                return
            for http_event in http_events:
                exchange = self._exchanges.get(http_event.stream_id)
                if exchange is None:
                    continue
                if isinstance(http_event, HeadersReceived):
                    exchange.headers_event(http_event.headers, http_event.stream_ended)
                elif isinstance(http_event, DataReceived):
                    exchange.data_event(http_event.data, http_event.stream_ended)
                if exchange.done:
                    self._exchanges.pop(http_event.stream_id, None)

        def discard(self, stream_id: int) -> None:
            exchange = self._exchanges.pop(stream_id, None)
            if exchange is None:
                return
            if not exchange.done:
                try:
                    self._quic.reset_stream(stream_id, error_code=H3_REQUEST_CANCELLED)
                    self.transmit()
                except Exception:
                    pass
            exchange.finish()

    class _SyncStream(httpx.SyncByteStream):
        """Bridge an exchange's body queue to httpx's synchronous reader."""

        def __init__(
            self,
            exchange: _Exchange,
            client: "_OriginClient",
            read_timeout: float,
        ) -> None:
            self._exchange = exchange
            self._client = client
            self._read_timeout = read_timeout
            self._closed = False

        def __iter__(self):
            try:
                while True:
                    try:
                        chunk = self._exchange.chunks.get(timeout=self._read_timeout)
                    except queue.Empty:
                        raise httpx.ReadTimeout(
                            "HTTP/3 body read timed out"
                        ) from None
                    if chunk is None:
                        break
                    yield chunk
                if self._exchange.error is not None:
                    raise self._exchange.error
            finally:
                self.close()

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            self._client.discard(self._exchange)

    class _OriginClient:
        """A QUIC connection to one origin, with its own asyncio loop."""

        def __init__(
            self,
            host: str,
            port: int,
            *,
            verify: bool,
            connect_timeout: float,
            read_timeout: float,
            idle_timeout: float,
        ) -> None:
            self.host = host
            self.port = port
            self.verify = verify
            self.connect_timeout = connect_timeout
            self.read_timeout = read_timeout
            self.idle_timeout = idle_timeout
            self._ready = threading.Event()
            self._closed = threading.Event()
            self._loop: asyncio.AbstractEventLoop | None = None
            self._protocol: _H3Protocol | None = None
            self._lost: asyncio.Event | None = None
            self._task: asyncio.Task | None = None
            self._last_error: BaseException | None = None
            self._thread = threading.Thread(
                target=self._run, name=f"http3-{host}:{port}", daemon=True
            )
            self._thread.start()

        # -- loop thread -------------------------------------------------
        def _run(self) -> None:
            loop = asyncio.new_event_loop()
            loop.set_exception_handler(self._loop_exception_handler)
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._supervise())
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                finally:
                    loop.close()

        @staticmethod
        def _loop_exception_handler(
            loop: asyncio.AbstractEventLoop, context: dict
        ) -> None:
            # aioquic's connect() awaits wait_connected() through asyncio.shield.
            # Aborting a handshake cancels the outer task; the shielded future
            # then finishes with ConnectionError that nobody can retrieve.  It
            # is an artefact of the abort, not a download failure.
            if "shielded future" in str(context.get("message", "")):
                return
            loop.default_exception_handler(context)

        def _configuration(self) -> "QuicConfiguration":
            config = QuicConfiguration(
                is_client=True,
                alpn_protocols=H3_ALPN,
                idle_timeout=self.idle_timeout,
            )
            config.server_name = self.host
            if not self.verify:
                config.verify_mode = ssl.CERT_NONE
            return config

        async def _supervise(self) -> None:
            self._task = asyncio.current_task()
            try:
                await self._connection_loop()
            except asyncio.CancelledError:
                # Cancellation is how close() aborts a handshake that is still
                # waiting for a server which will never answer.
                if not self._closed.is_set():
                    raise

        async def _connection_loop(self) -> None:
            config = self._configuration()
            while not self._closed.is_set():
                lost = asyncio.Event()
                self._lost = lost
                try:
                    async with _quic_connect(
                        self.host,
                        self.port,
                        configuration=config,
                        create_protocol=_H3Protocol,
                        wait_connected=True,
                    ) as protocol:
                        protocol.terminated = lost
                        self._protocol = protocol
                        self._last_error = None
                        self._ready.set()
                        await lost.wait()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    self._last_error = exc
                finally:
                    self._ready.clear()
                    self._protocol = None
                    self._lost = None
                if not self._closed.is_set():
                    # A dropped connection reconnects on the next request.
                    await asyncio.sleep(0.1)

        # -- worker-thread side -----------------------------------------
        def request(
            self,
            method: bytes,
            target: bytes,
            authority: bytes,
            headers: list[tuple[bytes, bytes]],
        ) -> _Exchange:
            if self._closed.is_set():
                raise httpx.ConnectError("HTTP/3 client is closed")
            if not self._ready.wait(self.connect_timeout):
                detail = (
                    f": {self._last_error}" if self._last_error is not None else ""
                )
                raise httpx.ConnectTimeout(
                    f"HTTP/3 handshake with {self.host}:{self.port} timed out{detail}"
                )
            protocol = self._protocol
            loop = self._loop
            if protocol is None or loop is None or loop.is_closed():
                raise httpx.ConnectError(
                    f"HTTP/3 connection to {self.host}:{self.port} is not available"
                )
            future = asyncio.run_coroutine_threadsafe(
                protocol.request(
                    method, target, authority, headers, self.read_timeout
                ),
                loop,
            )
            try:
                return future.result(self.read_timeout + 5.0)
            except TimeoutError:
                future.cancel()
                raise httpx.ReadTimeout("HTTP/3 response timed out") from None
            except httpx.HTTPError:
                raise
            except BaseException as exc:
                if not self._ready.is_set():
                    raise httpx.ConnectError(
                        f"HTTP/3 connection failed: {exc}"
                    ) from exc
                raise httpx.ReadError(f"HTTP/3 request failed: {exc}") from exc

        def discard(self, exchange: _Exchange) -> None:
            if exchange.done:
                return
            protocol = self._protocol
            loop = self._loop
            if protocol is None or loop is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(protocol.discard, exchange.stream_id)
            except RuntimeError:
                pass

        def close(self) -> None:
            if self._closed.is_set():
                return
            self._closed.set()
            loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._wake)
                except RuntimeError:
                    pass
            # close() can be called from the GUI thread during cancel; never
            # block it for long on a wedged event loop (the thread is daemon).
            self._thread.join(timeout=1.0)

        def _wake(self) -> None:
            protocol = self._protocol
            lost = self._lost
            if lost is not None:
                lost.set()
            if protocol is not None:
                try:
                    protocol.close()
                except Exception:
                    pass
            task = self._task
            if protocol is None and task is not None and not task.done():
                # Still in the handshake: cancel instead of waiting for the
                # QUIC idle timeout.
                task.cancel()

    class HTTP3Transport(httpx.BaseTransport):
        def __init__(
            self,
            *,
            verify: bool = True,
            connect_timeout: float = 15.0,
            read_timeout: float = 60.0,
            idle_timeout: float = 60.0,
        ) -> None:
            self.verify = verify
            self.connect_timeout = connect_timeout
            self.read_timeout = read_timeout
            self.idle_timeout = idle_timeout
            self._clients: dict[tuple[str, int], _OriginClient] = {}
            self._lock = threading.Lock()

        def _client_for(self, url: httpx.URL) -> _OriginClient:
            host = url.host or ""
            port = url.port or 443
            key = (host, port)
            with self._lock:
                client = self._clients.get(key)
                if client is None:
                    client = _OriginClient(
                        host,
                        port,
                        verify=self.verify,
                        connect_timeout=self.connect_timeout,
                        read_timeout=self.read_timeout,
                        idle_timeout=self.idle_timeout,
                    )
                    self._clients[key] = client
                return client

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.scheme != "https":
                raise httpx.UnsupportedProtocol(
                    f"HTTP/3 requires https:// URLs, got {url.scheme}://"
                )
            if request.method not in ("GET", "HEAD"):
                raise httpx.UnsupportedProtocol(
                    "HTTP/3 transport only supports GET and HEAD requests"
                )
            authority = url.netloc or url.host.encode("ascii")
            target = url.raw_path or b"/"
            headers = [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in request.headers.multi_items()
                if name.lower() not in _SKIP_HEADERS
            ]
            client = self._client_for(url)
            exchange = client.request(
                request.method.encode("ascii"), target, authority, headers
            )
            if exchange.status is None:
                raise exchange.error or httpx.RemoteProtocolError(
                    "HTTP/3 response has no status"
                )
            return httpx.Response(
                status_code=exchange.status,
                headers=exchange.headers,
                stream=_SyncStream(exchange, client, self.read_timeout),
                extensions={"http_version": b"HTTP/3"},
                request=request,
            )

        def close(self) -> None:
            with self._lock:
                clients = list(self._clients.values())
                self._clients.clear()
            for client in clients:
                client.close()

else:  # pragma: no cover - exercised only without aioquic installed

    class HTTP3Transport(httpx.BaseTransport):  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise HTTP3Unavailable(
                "HTTP/3 needs the optional 'aioquic' package. "
                "Install with: pip install aioquic"
            )


def make_transport(
    *,
    verify: bool = True,
    connect_timeout: float = 15.0,
    read_timeout: float = 60.0,
    idle_timeout: float = 60.0,
) -> "HTTP3Transport":
    if not AVAILABLE:
        raise HTTP3Unavailable(
            "HTTP/3 needs the optional 'aioquic' package. "
            "Install with: pip install aioquic"
        )
    return HTTP3Transport(
        verify=verify,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        idle_timeout=idle_timeout,
    )
