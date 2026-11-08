"""Command line interface.

Examples:
    pdm download https://host/big.bin -n 16 -o big.bin
    pdm download https://host/big.bin --limit 10M --sha256 <hex>
    pdm probe https://host/big.bin --json
    pdm download URL --resume            # default is resume
    pdm download URL --restart           # discard partial state
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence
from urllib.parse import urlsplit

from . import __version__
from .downloader import Downloader
from .errors import PdmError
from .headers import format_size, parse_size
from .models import DownloadConfig
from .probe import probe as probe_url

log = logging.getLogger(__name__)


def _network_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-agent", default=None, help="override User-Agent")
    parser.add_argument("--referer", default=None, help="send Referer")
    parser.add_argument("--cookie", default=None, help="raw Cookie header")
    parser.add_argument(
        "--auth",
        default=None,
        metavar="USER:PASSWORD",
        help="HTTP authentication credentials",
    )
    parser.add_argument(
        "--auth-type", choices=("basic", "digest"), default="basic"
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="http://host:port or socks5://host:port",
    )
    parser.add_argument(
        "--insecure", action="store_true", help="do not verify TLS certificates"
    )
    parser.add_argument(
        "--ftp-active",
        action="store_true",
        help="use FTP active mode (PORT/EPRT; server connects back) instead of passive",
    )
    parser.add_argument(
        "--http2", action="store_true", help="offer HTTP/2 (experimental)"
    )
    parser.add_argument(
        "--http3",
        action="store_true",
        help="use HTTP/3 over QUIC (experimental; needs aioquic, no proxy support)",
    )
    parser.add_argument(
        "--sftp-key",
        default=None,
        metavar="FILE",
        help="SFTP private key (instead of a password)",
    )
    parser.add_argument(
        "--sftp-key-pass",
        default=None,
        metavar="TEXT",
        help="passphrase for --sftp-key",
    )
    parser.add_argument(
        "--sftp-known-hosts",
        default=None,
        metavar="FILE",
        help="known_hosts file for SFTP host-key verification",
    )
    parser.add_argument(
        "--sftp-auto-host-key",
        action="store_true",
        help="accept unknown SFTP host keys (insecure; first-connection only)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="NAME: VALUE",
        help="extra request header (repeatable)",
    )


def _add_transfer_options(
    parser: argparse.ArgumentParser,
    *,
    output_help: str = "output file path",
) -> None:
    """Engine options shared by ``download`` and ``batch``."""
    parser.add_argument("-o", "--output", default=None, help=output_help)
    parser.add_argument(
        "-n",
        "--connections",
        type=int,
        default=8,
        help="maximum simultaneous connections (1-64, default 8)",
    )
    parser.add_argument(
        "--min-segment", default="1M", help="minimum initial segment size"
    )
    parser.add_argument(
        "--min-split", default="1M", help="minimum size when splitting dynamically"
    )
    parser.add_argument(
        "--no-dynamic",
        action="store_true",
        help="disable dynamic re-segmentation",
    )
    parser.add_argument(
        "--adaptive",
        action="store_true",
        help="AIMD: start with few connections, ramp up while healthy and "
        "halve on 429/503 (upper bound is -n)",
    )
    parser.add_argument("--limit", default=None, help="speed limit, e.g. 5M")
    parser.add_argument(
        "--global-limit",
        default=None,
        help="application-wide speed limit shared by concurrent downloads, e.g. 10M",
    )
    parser.add_argument(
        "--max-host-connections",
        type=int,
        default=None,
        help="cap connections to one host (default: no extra cap)",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="JSON host policy file: per-host connections, limits, proxies and "
        "site credentials (User-Agent, Cookie, auth)",
    )
    parser.add_argument(
        "--mirror",
        action="append",
        default=[],
        metavar="URL",
        help="extra URL serving the identical file (repeatable); segments are "
        "spread across mirrors",
    )
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument(
        "--retry-delay", type=float, default=1.0, help="base backoff seconds"
    )
    parser.add_argument(
        "--restart", action="store_true", help="discard partial download state"
    )
    parser.add_argument(
        "--no-resume", action="store_true", help="never resume an existing part"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace an existing output file"
    )
    parser.add_argument(
        "--category",
        default="none",
        metavar="auto|none|NAME",
        help="save into a category subfolder (auto = by extension; NAME is a "
        "category name from the built-in table or --categories file)",
    )
    parser.add_argument(
        "--categories",
        default=None,
        metavar="FILE",
        help="category table JSON: a list of "
        '{"name", "folder", "extensions"} objects (overrides the built-in table)',
    )
    parser.add_argument(
        "--no-progress", action="store_true", help="disable the progress line"
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdm",
        description="Multi-connection, resumable HTTP(S) downloader",
    )
    parser.add_argument(
        "--version", action="version", version=f"pdm {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    network = argparse.ArgumentParser(add_help=False)
    _network_options(network)

    download = sub.add_parser(
        "download", parents=[network], help="download one URL"
    )
    download.add_argument("url")
    _add_transfer_options(download)
    download.add_argument(
        "--sha256", default=None, help="verify the finished file against this hash"
    )
    download.add_argument(
        "--json",
        action="store_true",
        help="emit progress snapshots and the final result as JSON lines",
    )

    batch = sub.add_parser(
        "batch",
        parents=[network],
        help="expand wildcard URL patterns ([1-10], [a-z], {a,b}) and download each",
    )
    batch.add_argument(
        "patterns",
        nargs="+",
        metavar="PATTERN",
        help="one or more patterns; use [1-10], [001-010], [a-z] or {a,b,c}",
    )
    _add_transfer_options(
        batch,
        output_help="destination directory (default: current directory)",
    )

    probe = sub.add_parser(
        "probe", parents=[network], help="print metadata and exit"
    )
    probe.add_argument("url")
    probe.add_argument("--json", action="store_true")
    probe.add_argument("-v", "--verbose", action="count", default=0)

    hls = sub.add_parser(
        "hls", parents=[network], help="download an HLS (m3u8) VOD stream"
    )
    hls.add_argument("url")
    hls.add_argument("-o", "--output", required=True, help="output file")
    hls.add_argument(
        "-n", "--connections", type=int, default=6, help="parallel segment downloads"
    )
    hls.add_argument(
        "--quality",
        default=None,
        help="variant selector for master playlists: best (default), worst, index, resolution",
    )
    _add_stream_options(hls)

    dash = sub.add_parser(
        "dash",
        parents=[network],
        help="download one unencrypted DASH (MPD) VOD track",
    )
    dash.add_argument("url")
    dash.add_argument("-o", "--output", required=True, help="output file")
    dash.add_argument(
        "-n", "--connections", type=int, default=6, help="parallel segment downloads"
    )
    dash.add_argument(
        "--quality",
        default=None,
        help="representation selector: best (default), worst, index, height",
    )
    dash.add_argument(
        "--track",
        choices=("video", "audio", "both"),
        default="video",
        help=(
            "which track to download; 'both' fetches video+audio and merges "
            "them with ffmpeg when available"
        ),
    )
    dash.add_argument(
        "--ffmpeg",
        default=None,
        metavar="PATH",
        help=(
            "ffmpeg executable for --track both (default: PDM_DOWNLOADER_FFMPEG "
            "or ffmpeg on PATH)"
        ),
    )
    _add_stream_options(dash)

    update = sub.add_parser(
        "update", help="check a release manifest and optionally fetch the installer"
    )
    update.add_argument(
        "--manifest",
        required=True,
        metavar="URL",
        help="HTTPS URL of the update manifest (HTTP only for loopback tests)",
    )
    update.add_argument(
        "--download",
        default=None,
        metavar="DIR",
        help="download directory (default: %%LOCALAPPDATA%%\\PersonalDownloadManager\\updates)",
    )
    update.add_argument(
        "--launch",
        action="store_true",
        help="after a verified download, run the installer (explicit opt-in)",
    )
    update.add_argument("--json", action="store_true")
    update.add_argument("--timeout", type=float, default=30.0)

    sub.add_parser("gui", help="launch the PySide6 desktop UI")
    return parser


def _add_stream_options(parser: argparse.ArgumentParser) -> None:
    """Options shared by the ``hls`` and ``dash`` subcommands."""
    parser.add_argument("--limit", default=None, help="speed limit, e.g. 5M")
    parser.add_argument("--global-limit", default=None)
    parser.add_argument("--max-host-connections", type=int, default=None)
    parser.add_argument("--policy", default=None, help="JSON host policy file")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--sha256", default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)


def _parse_headers(items: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            raise SystemExit(f"invalid --header {item!r}, expected NAME: VALUE")
        name, value = item.split(":", 1)
        name = name.strip()
        if not name:
            raise SystemExit(f"invalid --header {item!r}, empty name")
        headers[name] = value.strip()
    return headers


def _parse_auth(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    if ":" not in value:
        raise SystemExit("--auth must be USER:PASSWORD")
    user, password = value.split(":", 1)
    return user, password


def _governor_from_args(args: argparse.Namespace):
    """Build the shared host/global policy for this process."""
    import json
    from pathlib import Path

    from .policy import Governor, HostPolicies

    if getattr(args, "policy", None):
        try:
            data = json.loads(
                Path(args.policy).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read --policy {args.policy}: {exc}") from exc
        policies = HostPolicies.from_dict(data)
    else:
        policies = HostPolicies()

    cap = getattr(args, "max_host_connections", None)
    if cap is not None:
        if cap < 0:
            raise ValueError("--max-host-connections must be >= 0")
        if cap == 0:
            cap = None
        policies = HostPolicies(
            policies.rules,
            default_connections=cap,
            default_speed_limit=policies.default_speed_limit,
            default_proxy=policies.default_proxy,
            proxy_groups=policies.proxy_groups,
        )

    return Governor(policies, parse_size(getattr(args, "global_limit", None)))


def _validate_protocol_flags(args: argparse.Namespace, url: str | None) -> None:
    """Reject protocol combinations the transports cannot represent."""
    scheme = (urlsplit(url).scheme.lower() if url else "")
    if scheme in ("ftp", "ftps", "sftp"):
        if getattr(args, "ftp_active", False) and scheme == "sftp":
            raise ValueError("--ftp-active only applies to ftp:// or ftps:// URLs")
        if getattr(args, "http2", False) or getattr(args, "http3", False):
            raise ValueError("--http2/--http3 only apply to http(s) URLs")
        if getattr(args, "proxy", None):
            raise ValueError(
                f"{scheme.upper()} cannot be combined with --proxy"
            )
        if getattr(args, "mirror", None):
            raise ValueError("--mirror only applies to http(s) downloads")
        return
    if getattr(args, "ftp_active", False):
        raise ValueError("--ftp-active only applies to ftp:// or ftps:// URLs")
    http3 = bool(getattr(args, "http3", False))
    if http3 and getattr(args, "http2", False):
        raise ValueError("--http2 and --http3 are mutually exclusive")
    if http3 and getattr(args, "proxy", None):
        raise ValueError(
            "--http3 cannot be combined with --proxy "
            "(CONNECT-UDP/MASQUE is not implemented)"
        )


def _config_from_args(
    args: argparse.Namespace,
    *,
    url: str | None = None,
    output: str | None = None,
) -> DownloadConfig:
    from .models import DEFAULT_USER_AGENT

    target_url = url if url is not None else args.url
    _validate_protocol_flags(args, target_url)
    return DownloadConfig(
        url=target_url,
        output=output if output is not None else args.output,
        max_connections=args.connections,
        min_segment_size=parse_size(args.min_segment) or 1024 * 1024,
        min_split_size=parse_size(args.min_split) or 1024 * 1024,
        dynamic_segmentation=not args.no_dynamic,
        adaptive=bool(getattr(args, "adaptive", False)),
        mirrors=list(getattr(args, "mirror", None) or []),
        max_retries=args.retries,
        retry_backoff=args.retry_delay,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        read_timeout=max(args.timeout, 30.0),
        speed_limit=parse_size(args.limit),
        user_agent=args.user_agent or DEFAULT_USER_AGENT,
        headers=_parse_headers(args.header),
        referer=args.referer,
        cookie=args.cookie,
        auth=_parse_auth(args.auth),
        auth_type=args.auth_type,
        proxy=args.proxy,
        verify=not args.insecure,
        http2=args.http2,
        http3=getattr(args, "http3", False),
        ftp_active=bool(getattr(args, "ftp_active", False)),
        sftp_key=getattr(args, "sftp_key", None),
        sftp_key_pass=getattr(args, "sftp_key_pass", None),
        sftp_known_hosts=getattr(args, "sftp_known_hosts", None),
        sftp_auto_host_key=bool(getattr(args, "sftp_auto_host_key", False)),
        resume=not args.no_resume,
        restart=args.restart,
        overwrite=args.overwrite,
        sha256=getattr(args, "sha256", None),
        show_progress=not args.no_progress and not args.quiet and not getattr(args, "json", False),
        json_progress=bool(getattr(args, "json", False)),
        quiet=args.quiet,
        log_level="DEBUG" if args.verbose else "INFO",
        governor=_governor_from_args(args),
    )


def _load_categories(path: str | None) -> list[dict]:
    """Load a ``--categories`` JSON table, or the built-in defaults."""
    import json
    from pathlib import Path

    from .categories import default_categories, normalize_categories

    if not path:
        return default_categories()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read --categories {path}: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("categories")
    if not isinstance(data, list):
        raise ValueError(
            "--categories must be a JSON list of {name, folder, extensions}"
        )
    return normalize_categories(data)


def _category_output(
    spec: str | None,
    url: str,
    base: str | None,
    categories_path: str | None = None,
) -> str | None:
    """Resolve ``--category`` against the base output, or ``None``.

    ``None`` means "no subfolder": the flag is ``none``, the extension does not
    match, or ``-o`` names an explicit file (which always wins).  Otherwise the
    returned path ends with a separator so the engine creates the directory and
    keeps the server-provided filename.
    """
    import os
    from pathlib import Path

    from .categories import category_names, target_dir
    from .headers import filename_from_url

    choice = str(spec or "none").strip()
    if choice.lower() in ("", "none"):
        return None
    categories = _load_categories(categories_path)
    if choice.lower() != "auto" and choice not in category_names(categories):
        raise ValueError(
            f"unknown category {spec!r}; use auto, none or one of: "
            + ", ".join(category_names(categories))
        )
    if base:
        path = Path(base)
        explicit_file = (
            not str(base).endswith(("/", "\\"))
            and not (path.exists() and path.is_dir())
        )
        if explicit_file:
            return None
        root = path
    else:
        root = Path.cwd()
    filename = filename_from_url(url) or "download"
    target = target_dir(root, filename, choice, categories, True)
    return str(target) + os.sep if target != root else None


def _setup_logging(verbose: int, quiet: bool) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )
    # httpx/h2/aioquic log every request or handshake at INFO; keep the CLI
    # output about our download.
    for noisy in ("httpx", "httpcore", "hpack", "quic", "aioquic", "paramiko"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _run_download(args: argparse.Namespace) -> int:
    try:
        output = _category_output(
            args.category, args.url, args.output, args.categories
        )
        config = _config_from_args(args, output=output)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    downloader = Downloader(config)
    try:
        result = downloader.run()
    except KeyboardInterrupt:
        downloader.cancel()
        print("cancelled", file=sys.stderr)
        return 130
    except PdmError as exc:
        if args.verbose:
            log.exception("download failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
    else:
        print(
            f"Downloaded {result.path} ({format_size(result.total_size)}) "
            f"in {result.elapsed:.1f}s at {format_size(result.avg_speed)}/s"
        )
        print(
            f"  segments: {result.segments_created}, "
            f"peak connections: {result.peak_connections}, "
            f"retries: {result.retries}, "
            f"resumed: {'yes' if result.resumed else 'no'}"
        )
        if result.sha256:
            print(f"  sha256: {result.sha256}")
    return 0


def _run_batch(args: argparse.Namespace) -> int:
    """Expand wildcard patterns and download every URL sequentially."""
    from pathlib import Path

    from .urlbatch import UrlBatchError, expand_url_patterns

    try:
        urls = expand_url_patterns(args.patterns)
    except UrlBatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not urls:
        print("error: no URLs matched the given patterns", file=sys.stderr)
        return 2

    base = Path(args.output).expanduser() if args.output else Path.cwd()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create {base}: {exc}", file=sys.stderr)
        return 1

    total = len(urls)
    done = failed = 0
    for index, url in enumerate(urls, start=1):
        try:
            target = _category_output(
                args.category, url, str(base), args.categories
            )
            config = _config_from_args(
                args, url=url, output=target or str(base)
            )
            result = Downloader(config).run()
        except KeyboardInterrupt:
            print("\ncancelled", file=sys.stderr)
            return 130
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except PdmError as exc:
            failed += 1
            print(f"[{index}/{total}] FAILED {url}: {exc}", file=sys.stderr)
            continue
        except OSError as exc:
            failed += 1
            print(f"[{index}/{total}] FAILED {url}: {exc}", file=sys.stderr)
            continue
        done += 1
        print(
            f"[{index}/{total}] {result.path} "
            f"({format_size(result.total_size)}, {result.elapsed:.1f}s)",
            flush=True,
        )

    print(
        f"batch: {done}/{total} succeeded, {failed} failed",
        file=sys.stderr if failed else sys.stdout,
    )
    return 0 if failed == 0 else 1


def _run_hls(args: argparse.Namespace) -> int:
    from .hls import download_hls

    try:
        config = _stream_config(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = download_hls(config, quality=args.quality)
    except PdmError as exc:
        if args.verbose:
            log.exception("HLS download failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    print(
        f"Downloaded {result.path} ({format_size(result.total_size)}) "
        f"in {result.elapsed:.1f}s at {format_size(result.avg_speed)}/s"
    )
    print(
        f"  segments: {result.segments_created}, "
        f"peak connections: {result.peak_connections}, "
        f"retries: {result.retries}"
    )
    if result.sha256:
        print(f"  sha256: {result.sha256}")
    return 0


def _run_dash(args: argparse.Namespace) -> int:
    from .dash import download_dash

    try:
        config = _stream_config(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = download_dash(
            config, quality=args.quality, track_kind=args.track
        )
    except PdmError as exc:
        if args.verbose:
            log.exception("DASH download failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    print(
        f"Downloaded {result.path} ({format_size(result.total_size)}) "
        f"in {result.elapsed:.1f}s at {format_size(result.avg_speed)}/s"
    )
    print(
        f"  segments: {result.segments_created}, "
        f"peak connections: {result.peak_connections}, "
        f"retries: {result.retries}"
    )
    if result.sha256:
        print(f"  sha256: {result.sha256}")
    for extra in result.extra_paths:
        print(f"  extra track: {extra}")
    if result.extra_paths:
        print(
            "  note: ffmpeg was not found; the DASH tracks were kept separately",
            file=sys.stderr,
        )
    return 0


def _stream_config(args: argparse.Namespace) -> DownloadConfig:
    """DownloadConfig shared by the ``hls`` and ``dash`` commands."""
    from .models import DEFAULT_USER_AGENT

    _validate_protocol_flags(args, args.url)
    if urlsplit(args.url).scheme.lower() not in ("http", "https"):
        raise ValueError("hls/dash require an http(s) URL")
    return DownloadConfig(
        url=args.url,
        output=args.output,
        max_connections=args.connections,
        speed_limit=parse_size(args.limit),
        user_agent=args.user_agent or DEFAULT_USER_AGENT,
        headers=_parse_headers(args.header),
        referer=args.referer,
        cookie=args.cookie,
        auth=_parse_auth(args.auth),
        auth_type=args.auth_type,
        proxy=args.proxy,
        verify=not args.insecure,
        http2=bool(getattr(args, "http2", False)),
        http3=bool(getattr(args, "http3", False)),
        sftp_key=getattr(args, "sftp_key", None),
        sftp_key_pass=getattr(args, "sftp_key_pass", None),
        sftp_known_hosts=getattr(args, "sftp_known_hosts", None),
        sftp_auto_host_key=bool(getattr(args, "sftp_auto_host_key", False)),
        ffmpeg_path=getattr(args, "ffmpeg", None),
        max_retries=args.retries,
        retry_backoff=args.retry_delay,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        read_timeout=max(args.timeout, 30.0),
        sha256=args.sha256,
        overwrite=args.overwrite,
        quiet=args.quiet,
        log_level="DEBUG" if args.verbose else "INFO",
        governor=_governor_from_args(args),
    )


def _run_probe(args: argparse.Namespace) -> int:
    from .models import DEFAULT_USER_AGENT

    try:
        _validate_protocol_flags(args, args.url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    config = DownloadConfig(
        url=args.url,
        user_agent=args.user_agent or DEFAULT_USER_AGENT,
        headers=_parse_headers(args.header),
        referer=args.referer,
        cookie=args.cookie,
        auth=_parse_auth(args.auth),
        auth_type=args.auth_type,
        proxy=args.proxy,
        verify=not args.insecure,
        http2=args.http2,
        http3=getattr(args, "http3", False),
        ftp_active=bool(getattr(args, "ftp_active", False)),
        sftp_key=getattr(args, "sftp_key", None),
        sftp_key_pass=getattr(args, "sftp_key_pass", None),
        sftp_known_hosts=getattr(args, "sftp_known_hosts", None),
        sftp_auto_host_key=bool(getattr(args, "sftp_auto_host_key", False)),
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
    )
    scheme = urlsplit(args.url).scheme.lower()
    try:
        if scheme in ("ftp", "ftps"):
            from .ftp import probe_ftp

            result = probe_ftp(config)
        elif scheme == "sftp":
            from .sftp import probe_sftp

            result = probe_sftp(config)
        else:
            result = probe_url(config)
    except PdmError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    print(f"URL:          {result.url}")
    print(f"Final URL:    {result.final_url}")
    print(f"Status:       {result.status}")
    print(
        "Size:         "
        + (f"{result.total_size} bytes ({format_size(result.total_size)})"
           if result.total_size is not None
           else "unknown")
    )
    print(f"Range:        {'yes' if result.ranges else 'no'}")
    print(f"Protocol:     {result.http_version or '-'}")
    print(f"ETag:         {result.etag or '-'}")
    print(f"Last-Modified:{result.last_modified or ' -'}")
    print(f"Content-Type: {result.content_type or '-'}")
    print(f"Filename:     {result.filename or '-'}")
    return 0


def _run_update(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import __version__
    from .update import (
        UpdateError,
        check_update,
        default_update_dir,
        download_update,
        launch_installer,
    )

    try:
        info = check_update(
            args.manifest, current_version=__version__, timeout=args.timeout
        )
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    downloaded: str | None = None
    launched = False
    if info is not None and (args.download or args.launch):
        target = Path(args.download) if args.download else default_update_dir()
        try:
            path = download_update(info, target, timeout=args.timeout)
        except UpdateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        downloaded = str(path)
        if args.launch:
            try:
                launch_installer(path)
            except UpdateError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            launched = True

    if args.json:
        print(
            json.dumps(
                {
                    "current_version": __version__,
                    "update": None
                    if info is None
                    else {
                        "version": info.version,
                        "url": info.url,
                        "sha256": info.sha256,
                        "size": info.size,
                        "notes": info.notes,
                        "page_url": info.page_url,
                        "published_at": info.published_at,
                    },
                    "downloaded": downloaded,
                    "launched": launched,
                },
                # ASCII-safe JSON: Windows pipes may use a non-UTF-8 console
                # code page, and a non-ASCII path would otherwise be written
                # with GBK bytes that no JSON consumer can decode.
                ensure_ascii=True,
            )
        )
        return 0

    if info is None:
        print(f"PDM {__version__} 已是最新版本。")
        return 0
    print(f"发现新版本：{info.version}（当前 {__version__}）")
    if info.published_at:
        print(f"发布时间：{info.published_at}")
    if info.size is not None:
        print(f"安装包大小：{format_size(info.size)}")
    print(f"下载地址：{info.url}")
    if info.notes:
        print("更新说明：")
        print(info.notes)
    if downloaded is not None:
        print(f"安装包已下载并通过 SHA-256 校验：{downloaded}")
    if launched:
        print("安装程序已启动。")
    elif info is not None and not args.launch:
        print("使用 --download DIR 下载，或加 --launch 在下载后运行安装程序。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", 0), getattr(args, "quiet", False))
    if args.command == "download":
        return _run_download(args)
    if args.command == "batch":
        return _run_batch(args)
    if args.command == "hls":
        return _run_hls(args)
    if args.command == "dash":
        return _run_dash(args)
    if args.command == "probe":
        return _run_probe(args)
    if args.command == "update":
        return _run_update(args)
    if args.command == "gui":
        try:
            from .gui.app import main as gui_main
        except ImportError:
            print(
                "error: PySide6 is not installed. "
                "Install the GUI extra: pip install \"personal-download-manager[gui]\"",
                file=sys.stderr,
            )
            return 2
        return gui_main([sys.argv[0]])
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
