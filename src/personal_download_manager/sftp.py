"""SFTP downloads over SSH (paramiko).

SFTP supports seeking, so resume is a normal ``seek(offset)`` on the remote
file.  The transport still uses the shared single-stream driver for the
journal, retries, limits and final rename.

paramiko is optional: the module imports without it and raises a clear
``FatalError`` when an SFTP URL is actually used.  Host keys are verified by
default (``RejectPolicy``); ``--sftp-auto-host-key`` and
``--sftp-known-hosts`` are the explicit escape hatches, matching OpenSSH's
"unknown host key" behaviour instead of silently trusting every server.
"""

from __future__ import annotations

import logging
import socket
from typing import Callable

from .errors import (
    AuthRequired,
    Cancelled,
    FatalError,
    PdmError,
    ProbeError,
    RetryableError,
)
from .models import DownloadConfig, ProbeResult
from .remote import (
    CancelRelay,
    RemoteLocation,
    RemoteMeta,
    check_remote_change,
    display_url,
    parse_remote_url,
    reject_proxy,
    remote_name,
    single_stream_download,
)

CHUNK = 256 * 1024

# paramiko logs every socket teardown at ERROR; failures already surface as
# engine exceptions with useful messages.  A NullHandler + no propagation also
# prevents "Logging error: I/O operation on closed file" from daemon transports
# that outlive their application's log streams.
_paramiko_log = logging.getLogger("paramiko")
_paramiko_log.setLevel(logging.CRITICAL)
_paramiko_log.addHandler(logging.NullHandler())
_paramiko_log.propagate = False


def _paramiko():
    try:
        import paramiko  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise FatalError(
            "SFTP needs the optional 'paramiko' package. "
            "Install with: pip install 'personal-download-manager[sftp]'"
        ) from exc
    return paramiko


def _load_private_key(config: DownloadConfig):
    if not config.sftp_key:
        return None
    paramiko = _paramiko()
    try:
        return paramiko.PKey.from_path(config.sftp_key, config.sftp_key_pass)
    except Exception as exc:
        raise FatalError(
            f"cannot load SFTP private key {config.sftp_key}: {exc}"
        ) from exc


def _map_connection_error(exc: BaseException, url: str) -> None:
    paramiko = _paramiko()
    if isinstance(exc, paramiko.AuthenticationException):
        raise AuthRequired(
            f"SFTP authentication failed for {url} (pass --auth user:password "
            "or --sftp-key)"
        ) from exc
    if isinstance(exc, paramiko.BadHostKeyException):
        raise FatalError(
            f"SFTP host key for {url} does not match known_hosts"
        ) from exc
    if isinstance(exc, paramiko.SSHException):
        text = str(exc)
        if "known_hosts" in text or "host key" in text.lower():
            raise FatalError(
                f"SFTP host key for {url} is not trusted: {text}. "
                "Pass --sftp-known-hosts FILE or (less safe) "
                "--sftp-auto-host-key"
            ) from exc
        raise RetryableError(f"SFTP connection failed for {url}: {text}") from exc
    if isinstance(exc, (socket.timeout, TimeoutError)):
        raise RetryableError(f"SFTP connection timed out for {url}") from exc
    if isinstance(exc, OSError):
        raise RetryableError(f"SFTP connection failed for {url}: {exc}") from exc
    raise exc


class _SftpSession:
    def __init__(self, config: DownloadConfig, location: RemoteLocation) -> None:
        self.config = config
        self.location = location
        self.meta: RemoteMeta | None = None

    # -- connection ------------------------------------------------------
    def _connect(self):
        paramiko = _paramiko()
        location = self.location
        username = location.username or (self.config.auth[0] if self.config.auth else None)
        password = location.password
        if password is None and self.config.auth:
            if location.username is None or location.username == self.config.auth[0]:
                password = self.config.auth[1]
        pkey = _load_private_key(self.config)
        if not username:
            raise FatalError(
                "SFTP URL must include a user name (sftp://user@host/path)"
            )
        if password is None and pkey is None:
            raise FatalError(
                "SFTP needs a password (--auth user:password) or --sftp-key"
            )

        client = paramiko.SSHClient()
        try:
            if self.config.sftp_known_hosts:
                try:
                    client.load_host_keys(self.config.sftp_known_hosts)
                except OSError as exc:
                    raise FatalError(
                        "cannot read SFTP known_hosts file "
                        f"{self.config.sftp_known_hosts}: {exc}"
                    ) from exc
            else:
                try:
                    client.load_system_host_keys()
                except OSError:
                    pass
            policy = (
                paramiko.AutoAddPolicy()
                if self.config.sftp_auto_host_key
                else paramiko.RejectPolicy()
            )
            client.set_missing_host_key_policy(policy)
            try:
                client.connect(
                    location.host,
                    location.port,
                    username=username,
                    password=password,
                    pkey=pkey,
                    timeout=self.config.connect_timeout,
                    banner_timeout=self.config.connect_timeout,
                    auth_timeout=self.config.connect_timeout,
                    allow_agent=False,
                    look_for_keys=False,
                )
            except PdmError:
                raise
            except BaseException as exc:
                _map_connection_error(exc, display_url(self.config.url))
            try:
                sftp = client.open_sftp()
            except PdmError:
                raise
            except BaseException as exc:
                _map_connection_error(exc, display_url(self.config.url))
            # A timed-out read leaves the SFTP packet stream unusable, so the
            # timeout means "reconnect and resume"; cancellation instead closes
            # the transport through CancelRelay (immediate).
            sftp.get_channel().settimeout(max(1.0, self.config.read_timeout))
            return client, sftp
        except BaseException:
            try:
                client.close()
            except BaseException:
                pass
            raise

    # -- metadata --------------------------------------------------------
    def _stat(self, sftp) -> RemoteMeta:
        try:
            attr = sftp.stat(self.location.path)
        except FileNotFoundError as exc:
            raise FatalError(f"SFTP file not found: {self.location.path}") from exc
        except OSError as exc:
            raise RetryableError(
                f"SFTP stat failed for {self.location.path}: {exc}"
            ) from exc
        mtime = str(int(attr.st_mtime)) if attr.st_mtime is not None else None
        return RemoteMeta(
            filename=remote_name(self.config, self.location.path),
            total_size=int(attr.st_size) if attr.st_size is not None else None,
            mtime=mtime,
        )

    def metadata(self) -> RemoteMeta:
        client, sftp = self._connect()
        try:
            self.meta = self._stat(sftp)
            return self.meta
        finally:
            try:
                sftp.close()
            finally:
                client.close()

    # -- transfer --------------------------------------------------------
    def read_from(
        self,
        offset: int,
        total: int | None,
        sink: Callable[[bytes], None],
        cancel_event=None,
        relay: CancelRelay | None = None,
    ) -> None:
        client, sftp = self._connect()

        def closer() -> None:
            try:
                client.close()
            except BaseException:
                pass

        if relay is not None:
            relay.register(closer)
        handle = None
        try:
            current = self._stat(sftp)
            if self.meta is not None:
                check_remote_change(self.meta, current.total_size, current.mtime)
            try:
                handle = sftp.open(self.location.path, "rb")
                handle.seek(offset)
            except FileNotFoundError as exc:
                raise FatalError(
                    f"SFTP file not found: {self.location.path}"
                ) from exc
            except OSError as exc:
                raise RetryableError(
                    f"SFTP open failed for {self.location.path}: {exc}"
                ) from exc
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled("download cancelled")
                try:
                    chunk = handle.read(CHUNK)
                except (socket.timeout, TimeoutError) as exc:
                    # The packet stream may be out of sync after a timeout;
                    # reconnect from the last written offset instead of retrying
                    # the read on the same channel.
                    raise RetryableError(
                        "SFTP read timed out for "
                        f"{display_url(self.config.url)}"
                    ) from exc
                except OSError as exc:
                    raise RetryableError(
                        f"SFTP read failed for "
                        f"{display_url(self.config.url)}: {exc}"
                    ) from exc
                if not chunk:
                    break
                sink(chunk)
        finally:
            if relay is not None:
                relay.unregister(closer)
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            try:
                sftp.close()
            finally:
                client.close()


def _location(config: DownloadConfig) -> RemoteLocation:
    reject_proxy(config, "SFTP")
    return parse_remote_url(config, default_port=22, schemes=("sftp",))


def _probe_meta(session: _SftpSession) -> RemoteMeta:
    try:
        return session.metadata()
    except AuthRequired:
        raise
    except RetryableError as exc:
        raise ProbeError(str(exc), transient=True) from exc
    except FatalError as exc:
        raise ProbeError(str(exc)) from exc


def probe_sftp(config: DownloadConfig) -> ProbeResult:
    location = _location(config)
    session = _SftpSession(config, location)
    meta = _probe_meta(session)
    return ProbeResult(
        url=config.url,
        final_url=config.url,
        status=200,
        total_size=meta.total_size,
        ranges=False,
        last_modified=meta.mtime,
        filename=meta.filename,
        http_version="SFTP",
    )


def download_sftp(
    config: DownloadConfig,
    cancel_event=None,
    progress_callback=None,
):
    location = _location(config)
    session = _SftpSession(config, location)
    meta = _probe_meta(session)
    relay = CancelRelay(cancel_event).start()
    try:
        return single_stream_download(
            config,
            mode=location.scheme,
            meta=meta,
            read_from=lambda offset, total, sink: session.read_from(
                offset, total, sink, relay, relay
            ),
            cancel_event=relay,
            progress_callback=progress_callback,
        )
    finally:
        relay.stop()
