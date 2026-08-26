#!/usr/bin/env python3
"""Live MJPEG viewer for an RTSP camera, over HTTP.

Transcodes an RTSP stream to MJPEG with ffmpeg and serves it as
multipart/x-mixed-replace, which every browser renders natively in an <img>.
No JS, no dependencies beyond ffmpeg and the standard library.

    python3 camera_viewer.py --rtsp-url rtsp://192.168.100.123/full1080p

Then open http://127.0.0.1:8765/

This is a sanity check on the camera path, not part of the test matrix: it
proves the stream is reachable and decodable before wiring RTSP into the
harness. The matrix's video source is the synthetic pattern (see
teleop-test-matrix/src/video.rs for why).
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUNDARY = "frameboundary"

# A stalled RTSP read looks identical to a slow one: ffmpeg keeps the pipe open
# and simply stops emitting. Without a deadline the reader blocks forever and the
# page hangs with no error, so every read is bounded and a stall is reported.
STALL_TIMEOUT_S = 15.0

INDEX = b"""<!doctype html>
<meta charset="utf-8">
<title>camera</title>
<style>
  body { margin:0; background:#111; color:#ccc; font:14px system-ui; }
  img  { display:block; max-width:100vw; max-height:100vh; margin:0 auto; }
  p    { padding:8px 12px; margin:0; }
</style>
<img src="/stream" alt="camera stream">
<p>If this stays blank, the RTSP source is unreachable or not decoding &mdash; check the terminal.</p>
"""


def ffmpeg_cmd(rtsp_url: str, transport: str, fps: int, width: int | None) -> list[str]:
    """Build the ffmpeg invocation producing a raw MJPEG stream on stdout.

    TCP transport is the default because UDP RTSP silently drops frames on a
    congested or filtered path, which reads as a broken camera rather than a
    network problem.
    """
    scale = ["-vf", f"scale={width}:-2"] if width else []
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-rtsp_transport", transport,
        "-i", rtsp_url,
        "-an",                      # no audio: this is a video sanity check
        "-r", str(fps),
        *scale,
        "-f", "mpjpeg",
        "-q:v", "5",
        "pipe:1",
    ]


class Handler(BaseHTTPRequestHandler):
    rtsp_url = ""
    transport = "tcp"
    fps = 10
    width: int | None = None

    def log_message(self, fmt: str, *args) -> None:  # quieter than the default
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(INDEX)))
            self.end_headers()
            self.wfile.write(INDEX)
            return
        if self.path == "/stream":
            self._stream()
            return
        self.send_error(404)

    def _stream(self) -> None:
        cmd = ffmpeg_cmd(self.rtsp_url, self.transport, self.fps, self.width)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # ffmpeg's own diagnostics are the only place an RTSP failure is
        # explained; surface them rather than letting the browser show a blank
        # frame with no reason.
        def drain_stderr() -> None:
            for line in iter(proc.stderr.readline, b""):
                sys.stderr.write(f"[ffmpeg] {line.decode(errors='replace')}")

        threading.Thread(target=drain_stderr, daemon=True).start()

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}"
        )
        self.end_headers()

        # ffmpeg's mpjpeg muxer already emits multipart with its own boundary, so
        # the bytes are forwarded verbatim rather than reframed.
        try:
            while True:
                chunk = self._read_with_timeout(proc.stdout, 32768)
                if chunk is None:
                    sys.stderr.write(
                        f"[viewer] no data for {STALL_TIMEOUT_S:.0f}s; "
                        "RTSP source stalled\n"
                    )
                    break
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer closed the tab
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    @staticmethod
    def _read_with_timeout(stream, size: int) -> bytes | None:
        """Read up to `size` bytes, or None if nothing arrived before the deadline."""
        result: list[bytes] = []

        def do_read() -> None:
            result.append(stream.read(size))

        t = threading.Thread(target=do_read, daemon=True)
        t.start()
        t.join(STALL_TIMEOUT_S)
        return result[0] if result else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rtsp-url", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument(
        "--transport",
        default="tcp",
        choices=["tcp", "udp"],
        help="RTSP transport; tcp avoids silent frame loss on a filtered path",
    )
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--width", type=int, help="downscale for a lighter preview")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 2

    Handler.rtsp_url = args.rtsp_url
    Handler.transport = args.transport
    Handler.fps = args.fps
    Handler.width = args.width

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    signal.signal(signal.SIGINT, lambda *_: srv.shutdown())
    print(f"serving {args.rtsp_url}  ->  http://{args.bind}:{args.port}/")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
