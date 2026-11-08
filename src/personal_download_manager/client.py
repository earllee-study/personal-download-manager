"""HTTP client construction and header policy.

We use httpx for exact control over every header and connection while leaving
TLS, proxies and connection pooling to a well-tested implementation.

Two policies are deliberate:
  * ``Accept-Encoding: identity`` - a compressed body would make byte offsets
    meaningless, so range requests must never negotiate gzip/br.
  * one Client (and thus one TCP/TLS connection) per worker - segment workers
    must not queue behind each other in a shared pool.
"""

from __future__ import annotations

import base64
from dataclasses import replace

import httpx

from .errors import FatalError
from .headers import validate_header
from .models import DownloadConfig
from .policy import ResolvedPolicy


def apply_policy(config: DownloadConfig, policy: ResolvedPolicy) -> DownloadConfig:
    """Return ``config`` with a host rule's proxy/UA/Cookie/auth applied.

    Site rules win over the task's own values; anything the rule leaves unset
    keeps the task-level option, which in turn falls back to the application
    defaults.  The same helper is used for the primary host, mirrors, and HLS
    playlists/segments so all request paths behave identically.
    """
    overrides: dict[str, object] = {}
    proxy = policy.proxy or config.proxy
    if proxy != config.proxy:
        overrides["proxy"] = proxy
    if policy.user_agent and policy.user_agent != config.user_agent:
        overrides["user_agent"] = policy.user_agent
    if policy.cookie and policy.cookie != config.cookie:
        overrides["cookie"] = policy.cookie
    if policy.auth and policy.auth != config.auth:
        overrides["auth"] = policy.auth
        overrides["auth_type"] = policy.auth_type
    if not overrides:
        return config
    return replace(config, **overrides)


def build_headers(
    config: DownloadConfig, extra: dict[str, str] | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": config.user_agent,
        "Accept": "*/*",
        # Critical for byte-exact range handling.
        "Accept-Encoding": "identity",
    }
    if not (config.http2 or config.http3):
        # Connection-specific headers are illegal in HTTP/2 and HTTP/3.
        headers["Connection"] = "Keep-Alive"

    if config.referer:
        headers["Referer"] = config.referer
    if config.cookie:
        headers["Cookie"] = config.cookie
    if config.auth and config.auth_type == "basic":
        user, password = config.auth
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    for name, value in config.headers.items():
        validate_header(name, value)
        headers[name] = value
    if extra:
        for name, value in extra.items():
            validate_header(name, value)
            headers[name] = value
    return headers


def make_client(
    config: DownloadConfig,
    *,
    cookies: dict[str, str] | None = None,
    max_connections: int = 1,
) -> httpx.Client:
    """Create one client.  A worker owns its client for its whole lifetime."""
    kwargs: dict[str, object] = {
        "follow_redirects": True,
        "verify": config.verify,
        # Do not silently inherit HTTP(S)_PROXY from the environment; the CLI
        # has an explicit --proxy switch, which keeps benchmarks reproducible.
        "trust_env": False,
        "timeout": httpx.Timeout(
            config.timeout,
            connect=config.connect_timeout,
            read=config.read_timeout,
            write=config.read_timeout,
            pool=config.timeout,
        ),
        "limits": httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=30.0,
        ),
        "headers": build_headers(config),
    }
    if config.http2:
        kwargs["http2"] = True
    if config.http3:
        # HTTP/3 is its own transport on one shared QUIC connection; httpx's
        # h1/h2 machinery and pools are bypassed entirely.
        if config.proxy:
            raise FatalError(
                "HTTP/3 cannot be combined with a proxy (CONNECT-UDP/MASQUE "
                "is not implemented)"
            )
        if not isinstance(config.verify, bool):
            # The QUIC transport only understands verify=True/False; silently
            # ignoring a custom SSLContext would change which CAs are trusted.
            raise FatalError(
                "HTTP/3 supports verify=True/False, not a custom SSLContext"
            )
        from .http3 import make_transport

        kwargs.pop("limits", None)
        kwargs.pop("http2", None)
        kwargs["transport"] = make_transport(
            verify=config.verify,
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
        )
    elif config.proxy:
        kwargs["proxy"] = config.proxy
    if config.auth and config.auth_type == "digest":
        kwargs["auth"] = httpx.DigestAuth(config.auth[0], config.auth[1])

    try:
        client = httpx.Client(**kwargs)  # type: ignore[arg-type]
    except ImportError as exc:  # missing h2 / socksio
        raise FatalError(
            f"missing optional dependency for this network option: {exc}. "
            "Install with: pip install 'personal-download-manager[full]'"
        ) from exc
    except (ValueError, TypeError) as exc:
        raise FatalError(f"invalid HTTP client configuration: {exc}") from exc

    if cookies:
        client.cookies.update(cookies)
    return client


def redact_url(url: str) -> str:
    """Hide user:password in a URL before logging it."""
    try:
        parts = httpx.URL(url)
    except Exception:
        return url
    if parts.username or parts.password:
        return str(parts.copy_with(username="***", password="***"))
    return str(parts)
