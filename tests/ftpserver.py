"""An in-process FTP / explicit-FTPS server for tests and benchmarks.

pyftpdlib is a test-only dependency; it provides REST (resume), SIZE, MDTM and
TLS.  A per-server DummyAuthorizer mounts one temporary directory.  Logging is
silenced because the engine's own output is what the tests assert on.

    server = FtpServer(tmp_path, username="user", password="pw").start()
    server.url("/file.bin")            # ftp://user:pw@127.0.0.1:PORT/file.bin
    fts = FtpServer(tmp_path, tls=True).start()
    fts.url("/file.bin", scheme="ftps")
    ...
    server.stop()
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer

CERTS = Path(__file__).resolve().parent / "certs"

# pyftpdlib logs every command at INFO to the root logger; a NullHandler also
# avoids logging errors when its daemon threads outlive pytest's capture stream.
for _name in ("pyftpdlib",):
    _logger = logging.getLogger(_name)
    _logger.setLevel(logging.CRITICAL)
    _logger.addHandler(logging.NullHandler())
    _logger.propagate = False


class FtpServer:
    def __init__(
        self,
        root: str | Path,
        *,
        username: str = "user",
        password: str = "pw",
        tls: bool = False,
        allow_rest: bool = True,
        certfile: Path | str | None = None,
        keyfile: Path | str | None = None,
    ) -> None:
        self.root = Path(root)
        self.username = username
        self.password = password
        self.tls = tls
        self.allow_rest = allow_rest
        self.certfile = Path(certfile or CERTS / "localhost.crt")
        self.keyfile = Path(keyfile or CERTS / "localhost.key")
        self._server: FTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        # "PORT" / "EPRT" / "PASV" / "EPSV" per data-channel negotiation, so
        # tests can prove which mode the client actually used.
        self.modes: list[str] = []

    def start(self) -> "FtpServer":
        authorizer = DummyAuthorizer()
        authorizer.add_user(
            self.username, self.password, str(self.root), perm="elradfmwMT"
        )
        base_class = TLS_FTPHandler if self.tls else FTPHandler
        allow_rest = self.allow_rest
        modes = self.modes

        class _Handler(base_class):
            def ftp_REST(self, pos):
                if not allow_rest:
                    self.respond("502 REST not implemented")
                    return
                return super().ftp_REST(pos)

            def ftp_PORT(self, line):
                modes.append("PORT")
                return super().ftp_PORT(line)

            def ftp_EPRT(self, line):
                modes.append("EPRT")
                return super().ftp_EPRT(line)

            def ftp_PASV(self, line):
                modes.append("PASV")
                return super().ftp_PASV(line)

            def ftp_EPSV(self, line):
                modes.append("EPSV")
                return super().ftp_EPSV(line)

        handler_class = _Handler
        if self.tls:
            handler_class.certfile = str(self.certfile)
            handler_class.keyfile = str(self.keyfile)
        handler_class.authorizer = authorizer
        self._server = FTPServer(("127.0.0.1", 0), handler_class)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"timeout": 0.1},
            name="ftp-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.close_all()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def url(
        self,
        path: str,
        *,
        scheme: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> str:
        scheme = scheme or ("ftps" if self.tls else "ftp")
        user = username if username is not None else self.username
        secret = password if password is not None else self.password
        return f"{scheme}://{user}:{secret}@127.0.0.1:{self.port}{path}"
