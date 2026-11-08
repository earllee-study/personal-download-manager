"""A tiny lossy UDP relay for weak-network HTTP/3 experiments.

Loopback has no packet loss, so a plain h3/h2 benchmark cannot show the
behaviour QUIC was designed for.  This relay sits between the client and an
HTTP/3 test server on one UDP socket: it forwards datagrams and drops a
configurable percentage in both directions, which makes aioquic retransmit
exactly as it would on a lossy path.  It is deliberately single-client (the
engine opens one QUIC connection per origin).

    proxy = LossyUdpProxy(server.port, loss=0.02).start()
    url = proxy.url_for("/file.bin")
    ...
    proxy.stop()
"""

from __future__ import annotations

import random
import socket
import threading


class LossyUdpProxy:
    def __init__(
        self,
        target_port: int,
        *,
        target_host: str = "127.0.0.1",
        loss: float = 0.0,
        seed: int = 7,
    ) -> None:
        self.target = (target_host, int(target_port))
        self.loss = max(0.0, min(0.9, float(loss)))
        self.client: tuple[str, int] | None = None
        self.forwarded = 0
        self.dropped = 0
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self._thread: threading.Thread | None = None

    def start(self) -> "LossyUdpProxy":
        self._thread = threading.Thread(
            target=self._run, name="lossy-udp-proxy", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self._sock.close()
        except OSError:
            pass

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    def url_for(self, path: str) -> str:
        return f"https://127.0.0.1:{self.port}{path}"

    def _drop(self) -> bool:
        with self._lock:
            if self.loss and self._rng.random() < self.loss:
                self.dropped += 1
                return True
            self.forwarded += 1
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if addr[0] == self.target[0] and addr[1] == self.target[1]:
                client = self.client
                if client is not None and not self._drop():
                    try:
                        self._sock.sendto(data, client)
                    except OSError:
                        return
            else:
                self.client = addr
                if not self._drop():
                    try:
                        self._sock.sendto(data, self.target)
                    except OSError:
                        return
