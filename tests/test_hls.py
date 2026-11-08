"""HLS playlist parsing and VOD downloading."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from personal_download_manager.errors import FatalError
from personal_download_manager.hls import choose_variant, download_hls, parse_playlist
from personal_download_manager.models import DownloadConfig
from server import Route, make_content


def encrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def config(url: str, output, **overrides) -> DownloadConfig:
    defaults = dict(
        url=url,
        output=output,
        show_progress=False,
        quiet=True,
        retry_backoff=0.01,
        timeout=10.0,
        connect_timeout=5.0,
        max_retries=2,
    )
    defaults.update(overrides)
    return DownloadConfig(**defaults)


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def test_parse_master_and_choose_variant():
    text = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
high/index.m3u8
"""
    playlist = parse_playlist(text, "https://cdn.test/vod/master.m3u8")
    assert playlist.is_master
    assert [v.uri for v in playlist.variants] == [
        "https://cdn.test/vod/low/index.m3u8",
        "https://cdn.test/vod/high/index.m3u8",
    ]
    assert choose_variant(playlist.variants, None).bandwidth == 2400000
    assert choose_variant(playlist.variants, "worst").bandwidth == 800000
    assert choose_variant(playlist.variants, "0").bandwidth == 800000
    assert choose_variant(playlist.variants, "1280x720").bandwidth == 2400000
    with pytest.raises(FatalError):
        choose_variant(playlist.variants, "9")


def test_parse_media_with_map_and_byteranges():
    text = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"
#EXTINF:4.0,
#EXT-X-BYTERANGE:1000@720
stream.mp4
#EXTINF:4.0,
#EXT-X-BYTERANGE:2000
stream.mp4
#EXT-X-ENDLIST
"""
    playlist = parse_playlist(text, "https://cdn.test/vod/media.m3u8")
    media = playlist.media
    assert media is not None
    assert media.ended is True
    assert media.init_uri == "https://cdn.test/vod/init.mp4"
    assert media.init_byterange == (720, 0)
    assert [s.byterange for s in media.segments] == [(1000, 720), (2000, 1720)]
    assert media.segments[1].uri == "https://cdn.test/vod/stream.mp4"


def test_parse_detects_encryption_and_live():
    encrypted = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090A0B0C0D0E0F
#EXTINF:4,
a.ts
#EXT-X-ENDLIST
"""
    media = parse_playlist(encrypted, "https://x/a.m3u8").media
    assert media is not None
    assert media.encrypted is True
    key = media.segments[0].key
    assert key is not None
    assert key.method == "AES-128"
    assert key.uri == "https://x/key.bin"
    assert key.iv == bytes(range(16))

    live = parse_playlist("#EXTM3U\n#EXTINF:4,\na.ts\n", "https://x/a.m3u8")
    assert live.media.ended is False


def test_refuses_sample_aes_and_drm_keyformats():
    sample = """#EXTM3U
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"
#EXTINF:1,
seg.ts
#EXT-X-ENDLIST
"""
    media = parse_playlist(sample, "https://x/a.m3u8").media
    assert media is not None and media.encryption_error
    assert "SAMPLE-AES" in media.encryption_error

    drm = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",KEYFORMAT="com.apple.streamingkeydelivery"
#EXTINF:1,
seg.ts
#EXT-X-ENDLIST
"""
    media = parse_playlist(drm, "https://x/a.m3u8").media
    assert media is not None and media.encryption_error
    assert "KEYFORMAT" in media.encryption_error


def test_sample_aes_download_is_refused(server, tmp_path):
    route = server.add("/drm/media.m3u8", Route(content=b""))
    route.content = (
        '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n'
        "#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    ).encode()
    with pytest.raises(FatalError, match="SAMPLE-AES"):
        download_hls(config(server.url("/drm/media.m3u8"), tmp_path / "drm.ts"))


def test_aes128_segments_with_explicit_iv(server, tmp_path):
    key = b"0123456789abcdef"
    iv = bytes(range(16))
    plain = [make_content(4096, seed=160), make_content(8192, seed=161)]
    server.add("/enc/key.bin", Route(content=key))
    server.add("/enc/seg0.ts", Route(content=encrypt_segment(plain[0], key, iv)))
    server.add("/enc/seg1.ts", Route(content=encrypt_segment(plain[1], key, iv)))
    playlist = server.add("/enc/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x{iv.hex()}\n'
        "#EXTINF:2,\nseg0.ts\n#EXTINF:2,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()
    output = tmp_path / "decrypted.ts"
    result = download_hls(
        config(server.url("/enc/media.m3u8"), output, max_connections=2)
    )
    assert output.read_bytes() == b"".join(plain)
    assert result.total_size == len(plain[0]) + len(plain[1])


def test_aes128_uses_media_sequence_as_default_iv(server, tmp_path):
    key = b"fedcba9876543210"
    sequence = 7
    plain = [make_content(3000, seed=162), make_content(5000, seed=163)]
    for index, data in enumerate(plain):
        iv = (sequence + index).to_bytes(16, "big")
        server.add(
            f"/seq/seg{index}.ts",
            Route(content=encrypt_segment(data, key, iv)),
        )
    server.add("/seq/key.bin", Route(content=key))
    playlist = server.add("/seq/media.m3u8", Route(content=b""))
    playlist.content = (
        f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{sequence}\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        "#EXTINF:2,\nseg0.ts\n#EXTINF:2,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()
    output = tmp_path / "sequence-iv.ts"
    download_hls(config(server.url("/seq/media.m3u8"), output))
    assert output.read_bytes() == b"".join(plain)


def test_aes128_key_rotation_and_method_none(server, tmp_path):
    key1 = b"1111111111111111"
    key2 = b"2222222222222222"
    iv1 = b"\x01" * 16
    iv2 = b"\x02" * 16
    plain = [
        make_content(2048, seed=164),
        make_content(3072, seed=165),
        make_content(4096, seed=166),
    ]
    server.add("/rot/k1.bin", Route(content=key1))
    server.add("/rot/k2.bin", Route(content=key2))
    server.add("/rot/s0.ts", Route(content=encrypt_segment(plain[0], key1, iv1)))
    server.add("/rot/s1.ts", Route(content=encrypt_segment(plain[1], key2, iv2)))
    server.add("/rot/s2.ts", Route(content=plain[2]))
    playlist = server.add("/rot/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD=AES-128,URI="k1.bin",IV=0x{iv1.hex()}\n'
        "#EXTINF:2,\ns0.ts\n"
        f'#EXT-X-KEY:METHOD=AES-128,URI="k2.bin",IV=0x{iv2.hex()}\n'
        "#EXTINF:2,\ns1.ts\n"
        "#EXT-X-KEY:METHOD=NONE\n"
        "#EXTINF:2,\ns2.ts\n#EXT-X-ENDLIST\n"
    ).encode()
    output = tmp_path / "rotation.ts"
    download_hls(config(server.url("/rot/media.m3u8"), output))
    assert output.read_bytes() == b"".join(plain)


def test_aes128_rejects_bad_key_length(server, tmp_path):
    key = b"short-key"
    iv = b"\x03" * 16
    server.add("/badkey/key.bin", Route(content=key))
    server.add("/badkey/seg.ts", Route(content=b"x" * 32))
    playlist = server.add("/badkey/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n"
        f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x{iv.hex()}\n'
        "#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    ).encode()
    with pytest.raises(FatalError, match="16"):
        download_hls(config(server.url("/badkey/media.m3u8"), tmp_path / "bad.ts"))


def test_live_playlist_is_refused(server, tmp_path):
    route = server.add("/live/media.m3u8", Route(content=b""))
    route.content = b"#EXTM3U\n#EXTINF:1,\nseg.ts\n"
    with pytest.raises(FatalError, match="live"):
        download_hls(config(server.url("/live/media.m3u8"), tmp_path / "live.ts"))


# ----------------------------------------------------------------------
# download
# ----------------------------------------------------------------------
def test_download_media_with_init_and_relative_uris(server, tmp_path):
    init = make_content(4096, seed=150)
    seg0 = make_content(8192, seed=151)
    seg1 = make_content(8192, seed=152)
    server.add("/vod/init.mp4", Route(content=init))
    server.add("/vod/seg0.ts", Route(content=seg0))
    server.add("/vod/seg1.ts", Route(content=seg1))
    playlist = server.add("/vod/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n"
        "#EXTINF:2,\nseg0.ts\n#EXTINF:2,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()

    result = download_hls(
        config(server.url("/vod/media.m3u8"), tmp_path / "out", max_connections=2)
    )

    assert result.path == tmp_path / "out.ts"
    assert result.path.read_bytes() == init + seg0 + seg1
    assert result.segments_created == 3
    assert result.total_size == len(init + seg0 + seg1)


def test_master_playlist_picks_best_variant(server, tmp_path):
    low = make_content(2048, seed=153)
    high = make_content(4096, seed=154)
    server.add("/m/low.ts", Route(content=low))
    server.add("/m/high.ts", Route(content=high))
    low_pl = server.add("/m/low.m3u8", Route(content=b""))
    low_pl.content = b"#EXTM3U\n#EXTINF:2,\nlow.ts\n#EXT-X-ENDLIST\n"
    high_pl = server.add("/m/high.m3u8", Route(content=b""))
    high_pl.content = b"#EXTM3U\n#EXTINF:2,\nhigh.ts\n#EXT-X-ENDLIST\n"
    master = server.add("/m/master.m3u8", Route(content=b""))
    master.content = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=900000\nhigh.m3u8\n"
    ).encode()

    download_hls(config(server.url("/m/master.m3u8"), tmp_path / "m.ts"))
    assert (tmp_path / "m.ts").read_bytes() == high


def test_byterange_playlist(server, tmp_path):
    stream = make_content(4096, seed=155)
    server.add("/br/stream.ts", Route(content=stream))
    playlist = server.add("/br/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n"
        "#EXTINF:1,\n#EXT-X-BYTERANGE:1000@0\nstream.ts\n"
        "#EXTINF:1,\n#EXT-X-BYTERANGE:1000@1000\nstream.ts\n"
        "#EXTINF:1,\n#EXT-X-BYTERANGE:1000@2000\nstream.ts\n"
        "#EXT-X-ENDLIST\n"
    ).encode()

    result = download_hls(
        config(server.url("/br/media.m3u8"), tmp_path / "br.ts", max_connections=2)
    )
    assert result.path.read_bytes() == stream[:3000]


def test_segments_download_concurrently(server, tmp_path):
    segment_size = 1024 * 1024
    for index in range(4):
        server.add(
            f"/fast/seg{index}.ts",
            Route(
                content=make_content(segment_size, seed=160 + index),
                throttle_bps=segment_size,
            ),
        )
    playlist = server.add("/fast/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n"
        + "".join(f"#EXTINF:1,\nseg{index}.ts\n" for index in range(4))
        + "#EXT-X-ENDLIST\n"
    ).encode()

    started = time.monotonic()
    result = download_hls(
        config(
            server.url("/fast/media.m3u8"),
            tmp_path / "fast.ts",
            max_connections=4,
        )
    )
    elapsed = time.monotonic() - started

    assert result.path.stat().st_size == 4 * segment_size
    assert result.peak_connections >= 2
    assert elapsed < 3.0, f"4 x 1 MiB segments took {elapsed:.1f}s; not parallel"


def test_cancel_stops_hls(server, tmp_path):
    import threading

    from personal_download_manager.errors import Cancelled

    for index in range(4):
        server.add(
            f"/cancel/seg{index}.ts",
            Route(
                content=make_content(512 * 1024, seed=180 + index),
                throttle_bps=256 * 1024,
            ),
        )
    playlist = server.add("/cancel/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n"
        + "".join(f"#EXTINF:1,\nseg{index}.ts\n" for index in range(4))
        + "#EXT-X-ENDLIST\n"
    ).encode()

    cancel = threading.Event()
    outcome: dict = {}

    def run() -> None:
        try:
            download_hls(
                config(server.url("/cancel/media.m3u8"), tmp_path / "c.ts"),
                cancel_event=cancel,
            )
        except Cancelled:
            outcome["cancelled"] = True
        except BaseException as exc:  # surface unexpected failures
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)
    cancel.set()
    thread.join(15)

    assert not thread.is_alive(), "HLS cancel did not stop the download"
    assert outcome.get("cancelled") is True, outcome
    assert not (tmp_path / "c.ts").exists()


def test_progress_callback_reports_bytes(server, tmp_path):
    for index in range(2):
        server.add(
            f"/prog/seg{index}.ts",
            Route(
                content=make_content(1024 * 1024, seed=190 + index),
                throttle_bps=1024 * 1024,
            ),
        )
    playlist = server.add("/prog/media.m3u8", Route(content=b""))
    playlist.content = (
        "#EXTM3U\n#EXTINF:1,\nseg0.ts\n#EXTINF:1,\nseg1.ts\n#EXT-X-ENDLIST\n"
    ).encode()

    payloads: list[dict] = []
    download_hls(
        config(server.url("/prog/media.m3u8"), tmp_path / "p.ts", max_connections=1),
        progress_callback=payloads.append,
    )

    assert payloads, "expected progress callbacks"
    assert max(p["downloaded"] for p in payloads) > 0
    assert all(p["segments"] == 2 for p in payloads)
    # The playlist has 2 x 1s segments, so the duration-based fraction moves.
    assert any(0.0 < p["fraction"] <= 1.0 for p in payloads)
    assert "eta" in payloads[0]


def test_sha256_verification(server, tmp_path):
    import hashlib

    data = make_content(2048, seed=170)
    server.add("/sha/seg.ts", Route(content=data))
    playlist = server.add("/sha/media.m3u8", Route(content=b""))
    playlist.content = b"#EXTM3U\n#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    digest = hashlib.sha256(data).hexdigest()

    download_hls(
        config(server.url("/sha/media.m3u8"), tmp_path / "sha.ts", sha256=digest)
    )
    with pytest.raises(Exception):
        download_hls(
            config(
                server.url("/sha/media.m3u8"),
                tmp_path / "sha2.ts",
                sha256="0" * 64,
            )
        )
