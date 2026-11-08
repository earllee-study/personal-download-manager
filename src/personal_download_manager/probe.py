"""Initial metadata probe.

Before opening the accelerator connections, the engine learns four things:
final URL, total size, whether the server honours Range, and a validator
(ETag / Last-Modified) it can send back as ``If-Range``.

We use ``GET Range: bytes=0-0`` instead of HEAD because a meaningful number of
servers return different (or no) metadata for HEAD.  The response body is never
consumed: only status and headers matter.
"""

from __future__ import annotations

import httpx

from .client import build_headers, make_client
from .errors import AuthRequired, HTTPStatusError, ProbeError
from .headers import (
    filename_from_url,
    parse_content_disposition,
    parse_content_range,
    parse_unsatisfied_range,
    sanitize_filename,
)
from .models import DownloadConfig, ProbeResult

_FALLBACK_STATUSES = {400, 405, 501}


def probe(config: DownloadConfig) -> ProbeResult:
    """Fetch metadata for ``config.url``; raises ProbeError on failure."""
    headers = build_headers(config, {"Range": "bytes=0-0"})
    try:
        with make_client(config, max_connections=2) as client:
            try:
                with client.stream("GET", config.url, headers=headers) as resp:
                    if resp.status_code in _FALLBACK_STATUSES:
                        result = _probe_without_range(client, config)
                    else:
                        result = _interpret(resp, config)
            except httpx.HTTPError:
                # A few servers refuse a GET probe outright; try HEAD once.
                result = _probe_without_range(client, config)
            result.cookies = dict(client.cookies)
            return result
    except httpx.InvalidURL as exc:
        raise ProbeError(f"invalid URL {config.url!r}: {exc}") from exc
    except httpx.UnsupportedProtocol as exc:
        raise ProbeError(f"unsupported URL scheme: {exc}") from exc
    except httpx.TooManyRedirects as exc:
        raise ProbeError(f"too many redirects for {config.url}") from exc
    except httpx.TimeoutException as exc:
        raise ProbeError(
            f"probe timed out for {config.url}: {exc}", transient=True
        ) from exc
    except httpx.TransportError as exc:
        raise ProbeError(
            f"cannot connect to {config.url}: {exc}", transient=True
        ) from exc
    except httpx.HTTPError as exc:
        raise ProbeError(
            f"probe failed for {config.url}: {exc}", transient=True
        ) from exc


def _probe_without_range(client: httpx.Client, config: DownloadConfig) -> ProbeResult:
    """HEAD fallback; if HEAD is rejected too, read a plain GET's headers."""
    try:
        with client.stream("HEAD", config.url, headers=build_headers(config)) as resp:
            if resp.status_code in (200, 206):
                return _interpret_head(resp, config)
    except httpx.HTTPError:
        pass

    # Last resort: plain GET, close without reading the body.
    with client.stream("GET", config.url, headers=build_headers(config)) as resp:
        return _interpret_plain(resp, config)


def _base_result(resp: httpx.Response, config: DownloadConfig) -> ProbeResult:
    final_url = str(resp.url)
    disposition = resp.headers.get("content-disposition")
    return ProbeResult(
        url=config.url,
        final_url=final_url,
        status=resp.status_code,
        total_size=None,
        ranges=False,
        etag=resp.headers.get("etag"),
        last_modified=resp.headers.get("last-modified"),
        content_type=resp.headers.get("content-type"),
        content_disposition=disposition,
        filename=_filename(disposition, final_url),
        accept_ranges=resp.headers.get("accept-ranges"),
        http_version=resp.http_version,
    )


def _filename(disposition: str | None, final_url: str) -> str | None:
    name = parse_content_disposition(disposition)
    if not name:
        name = filename_from_url(final_url)
    if not name:
        return None
    return sanitize_filename(name)


def _interpret(resp: httpx.Response, config: DownloadConfig) -> ProbeResult:
    result = _base_result(resp, config)
    status = resp.status_code

    if status == 206:
        cr = parse_content_range(resp.headers.get("content-range"))
        result.total_size = cr.total if cr else None
        result.ranges = cr is not None
        return result

    if status == 200:
        result.total_size = _content_length(resp)
        # A 200 to a Range request means the server ignored the range.
        result.ranges = False
        return result

    if status == 416:
        total = parse_unsatisfied_range(resp.headers.get("content-range"))
        result.total_size = total
        result.ranges = total == 0
        return result

    if status in (401, 407):
        challenge = resp.headers.get(
            "www-authenticate" if status == 401 else "proxy-authenticate", ""
        )
        raise AuthRequired(
            f"HTTP {status} authentication required"
            + (f" ({challenge})" if challenge else "")
            + " - pass --auth user:password"
        )
    if status == 403:
        raise HTTPStatusError(status, "Forbidden", str(resp.url))
    if status == 404:
        raise HTTPStatusError(status, "Not Found", str(resp.url))
    if status == 410:
        raise HTTPStatusError(status, "Gone", str(resp.url))
    if status == 429:
        raise ProbeError(
            f"HTTP 429 rate limited by {resp.url}; retry later or pass cookies",
            transient=True,
        )
    if status >= 500:
        raise ProbeError(f"HTTP {status} from {resp.url}", transient=True)
    if 300 <= status < 400:
        raise ProbeError(f"HTTP {status} redirect was not followed for {resp.url}")
    raise ProbeError(f"unexpected HTTP {status} from {resp.url}")


def _interpret_head(resp: httpx.Response, config: DownloadConfig) -> ProbeResult:
    result = _base_result(resp, config)
    result.total_size = _content_length(resp)
    accept = (result.accept_ranges or "").lower()
    result.ranges = "bytes" in accept and result.total_size is not None
    return result


def _interpret_plain(resp: httpx.Response, config: DownloadConfig) -> ProbeResult:
    result = _base_result(resp, config)
    if resp.status_code in (401, 407):
        challenge = resp.headers.get(
            "www-authenticate" if resp.status_code == 401 else "proxy-authenticate",
            "",
        )
        raise AuthRequired(
            f"HTTP {resp.status_code} authentication required"
            + (f" ({challenge})" if challenge else "")
        )
    if resp.status_code >= 400:
        raise ProbeError(
            f"HTTP {resp.status_code} from {resp.url}",
            transient=resp.status_code >= 500 or resp.status_code in (408, 425, 429),
        )
    result.total_size = _content_length(resp)
    # Accept-Ranges is a hint; the first real range request verifies it.
    result.ranges = "bytes" in (result.accept_ranges or "").lower()
    return result


def _content_length(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None
