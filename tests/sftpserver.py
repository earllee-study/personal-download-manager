"""A minimal in-process SFTP server for tests and benchmarks.

paramiko ships the server side too, so no external daemon is needed: this
starts a real SSH transport with a generated host key, accepts password and
(optionally) public-key authentication, and mounts an SFTP subsystem on a
local directory.  The downloader therefore talks real SSH/SFTP packets.

    server = SftpServer(tmp_path, username="user", password="pw").start()
    server.url("/file.bin")            # sftp://user:pw@127.0.0.1:PORT/file.bin
    ...
    server.stop()

Only the operations the client uses are implemented (stat/open/read); the
default paramiko handle does offset seeks.  Not a hardened server.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from pathlib import Path

import paramiko

# Server transports log socket teardown at ERROR, often after pytest has closed
# its capture streams; keep the test output clean.
_paramiko_log = logging.getLogger("paramiko")
_paramiko_log.setLevel(logging.CRITICAL)
_paramiko_log.addHandler(logging.NullHandler())
_paramiko_log.propagate = False


class _ServerInterface(paramiko.ServerInterface):
    def __init__(
        self,
        username: str,
        password: str | None,
        allowed_key: paramiko.PKey | None,
    ) -> None:
        self.username = username
        self.password = password
        self.allowed_key = allowed_key

    def check_auth_password(self, username: str, password: str) -> int:
        if username == self.username and self.password is not None:
            if password == self.password:
                return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if (
            username == self.username
            and self.allowed_key is not None
            and key.get_base64() == self.allowed_key.get_base64()
        ):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        methods = []
        if self.password is not None:
            methods.append("password")
        if self.allowed_key is not None:
            methods.append("publickey")
        return ",".join(methods) or "none"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _SFTPInterface(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root: str, **kwargs) -> None:
        super().__init__(server, *args, **kwargs)
        self.root = Path(root).resolve()

    def canonicalize(self, path: str) -> str:
        normal = os.path.normpath("/" + str(path).replace("\\", "/").lstrip("/"))
        return normal.replace("\\", "/")

    def _local(self, path: str) -> Path:
        relative = self.canonicalize(path).lstrip("/")
        full = (self.root / relative).resolve()
        if full != self.root and self.root not in full.parents:
            raise PermissionError("path escapes the SFTP root")
        return full

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._local(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._local(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        local = self._local(path)
        writable = flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)
        if writable:
            return paramiko.SFTP_OP_UNSUPPORTED
        try:
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = open(local, "rb")
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class SftpServer:
    def __init__(
        self,
        root: str | Path,
        *,
        username: str = "user",
        password: str | None = "pw",
        allowed_key: paramiko.PKey | None = None,
    ) -> None:
        self.root = Path(root)
        self.username = username
        self.password = password
        self.allowed_key = allowed_key
        self.host_key = paramiko.RSAKey.generate(2048)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._transports: list[paramiko.Transport] = []
        self._lock = threading.Lock()
        self.errors: list[str] = []

    # -- lifecycle ------------------------------------------------------
    def start(self) -> "SftpServer":
        self._thread = threading.Thread(
            target=self._accept_loop, name="sftp-server", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        with self._lock:
            transports = list(self._transports)
            self._transports.clear()
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def url(
        self,
        path: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> str:
        user = username if username is not None else self.username
        secret = password if password is not None else self.password
        if secret:
            return f"sftp://{user}:{secret}@127.0.0.1:{self.port}{path}"
        return f"sftp://{user}@127.0.0.1:{self.port}{path}"

    def known_hosts(self) -> str:
        return (
            f"[127.0.0.1]:{self.port} {self.host_key.get_name()} "
            f"{self.host_key.get_base64()}\n"
        )

    # -- accept / serve -------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve,
                args=(connection,),
                name="sftp-conn",
                daemon=True,
            ).start()

    def _serve(self, connection: socket.socket) -> None:
        transport: paramiko.Transport | None = None
        try:
            transport = paramiko.Transport(connection)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler(
                "sftp", paramiko.SFTPServer, _SFTPInterface, root=str(self.root)
            )
            server = _ServerInterface(self.username, self.password, self.allowed_key)
            with self._lock:
                self._transports.append(transport)
            transport.start_server(server=server)
            while transport.is_active() and not self._stop.is_set():
                time.sleep(0.05)
        except Exception as exc:  # surfaced through ``errors`` for debugging
            self.errors.append(repr(exc))
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
                with self._lock:
                    try:
                        self._transports.remove(transport)
                    except ValueError:
                        pass
