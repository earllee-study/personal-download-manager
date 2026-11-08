"""Local demo server for manual browser-extension verification.

Serves:
  * browser/test-page.html and browser/frame.html
  * a real AES-128 HLS playlist (synthetic segments) at /media/main.m3u8
    (also scripted/iframe/shadow aliases)
  * a small DASH manifest at /media/other.mpd
  * a plain MP4 at /media/plain.mp4
  * a small fragmented MP4 for the experimental MSE capture test page

The media is synthetic bytes, not playable video; the point is to verify that
the extension hands the URL to the native host and PDM downloads/decrypts
it end-to-end.

Usage:
    python browser/serve_demo.py
    python browser/serve_demo.py --port 8000
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]
KEY = b"0123456789abcdef"
IV = bytes(range(16))
SEGMENTS = [os.urandom(256 * 1024), os.urandom(256 * 1024), os.urandom(256 * 1024)]
TAKEOVER_BODY = os.urandom(128 * 1024)
COOKIE_BODY = os.urandom(64 * 1024)


def encrypt(data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    f'#EXT-X-KEY:METHOD=AES-128,URI="/media/key.bin",IV=0x{IV.hex()}\n'
    + "".join(
        f"#EXTINF:4.0,\nseg{i}.ts\n" for i in range(len(SEGMENTS))
    )
    + "#EXT-X-ENDLIST\n"
).encode()

LOW_PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    f'#EXT-X-KEY:METHOD=AES-128,URI="/media/key.bin",IV=0x{IV.hex()}\n'
    "#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
).encode()

MASTER_PLAYLIST = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
high.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low.m3u8
"""

MPD = b"""<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT2S" minBufferTime="PT1S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <SegmentList timescale="1" duration="2">
        <Initialization sourceURL="dash-init.mp4"/>
        <SegmentURL media="dash-seg.m4s"/>
      </SegmentList>
      <Representation id="v1" bandwidth="100000" width="320" height="180"/>
    </AdaptationSet>
  </Period>
</MPD>
"""

ROUTES = {
    "/media/main.m3u8": (PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/scripted.m3u8": (PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/iframe.m3u8": (PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/shadow.m3u8": (PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/master.m3u8": (MASTER_PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/high.m3u8": (PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/low.m3u8": (LOW_PLAYLIST, "application/vnd.apple.mpegurl"),
    "/media/key.bin": (KEY, "application/octet-stream"),
    **{
        f"/media/seg{i}.ts": (encrypt(data), "video/mp2t")
        for i, data in enumerate(SEGMENTS)
    },
    "/media/other.mpd": (MPD, "application/dash+xml"),
    "/media/dash-init.mp4": (b"\x00\x00\x00\x18ftypmp42", "video/mp4"),
    "/media/dash-seg.m4s": (os.urandom(128 * 1024), "video/mp4"),
    "/media/plain.mp4": (os.urandom(256 * 1024), "video/mp4"),
    "/media/takeover.bin": (TAKEOVER_BODY, "application/octet-stream"),
}

MSE_FIXTURE = Path(__file__).with_name("mse-fixture.mp4")
if MSE_FIXTURE.exists():
    ROUTES["/media/mse-demo.mp4"] = (MSE_FIXTURE.read_bytes(), "video/mp4")
    ROUTES["/media/mse-video.mp4"] = (MSE_FIXTURE.read_bytes(), "video/mp4")

MSE_AUDIO = Path(__file__).with_name("mse-audio.mp4")
if MSE_AUDIO.exists():
    ROUTES["/media/mse-audio.mp4"] = (MSE_AUDIO.read_bytes(), "audio/mp4")

MSE_WEBM_VIDEO = Path(__file__).with_name("mse-webm-video.webm")
MSE_WEBM_AUDIO = Path(__file__).with_name("mse-webm-audio.webm")
if MSE_WEBM_VIDEO.exists():
    ROUTES["/media/mse-webm-video.webm"] = (
        MSE_WEBM_VIDEO.read_bytes(),
        "video/webm",
    )
if MSE_WEBM_AUDIO.exists():
    ROUTES["/media/mse-webm-audio.webm"] = (
        MSE_WEBM_AUDIO.read_bytes(),
        "audio/webm",
    )

EXPECTED_HLS = b"".join(SEGMENTS)
EXPECTED_PLAIN = ROUTES["/media/plain.mp4"][0]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/media/cookie-check.bin":
            cookie = self.headers.get("Cookie") or ""
            print("cookie-check Cookie=%r" % cookie, file=sys.stderr)
            if "pdmcookie=verified" not in cookie:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(COOKIE_BODY)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(COOKIE_BODY)
            return
        route = ROUTES.get(path)
        if route is None:
            return super().do_GET()
        body, content_type = route
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if path == "/media/takeover.bin":
            self.send_header(
                "Content-Disposition", 'attachment; filename="takeover-demo.bin"'
            )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(
        (args.host, args.port), partial(Handler, directory=str(ROOT))
    )
    print(f"test page: http://{args.host}:{args.port}/browser/test-page.html", flush=True)
    print(f"HLS:       http://{args.host}:{args.port}/media/main.m3u8", flush=True)
    print(f"HLS size:  {len(EXPECTED_HLS)} bytes (decrypted)", flush=True)
    print(f"MP4 size:  {len(EXPECTED_PLAIN)} bytes", flush=True)
    print("Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
