"""HTTP API for the BWF clip service (standard library only).

Endpoints:
    POST /api/bwf/clip   multipart/form-data clip operation
    GET  /healthz        liveness/readiness probe
    GET  /version        build information baked into the image
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import __version__
from .bwf import MAX_WAV_BYTES, BwfError, clip_bwf
from .multipart import (
    MultipartError,
    parse_multipart,
    parse_uint,
    single_part,
)

# Request body ceiling: the 16 MiB WAV plus multipart framing overhead.
MAX_BODY_BYTES = MAX_WAV_BYTES + 1024 * 1024
# Bodies larger than this are rejected without being read (connection closed).
# Between MAX_BODY_BYTES and HARD_MAX_BODY_BYTES the body is drained first, so
# well-behaved clients can still read the 413 response off the connection.
HARD_MAX_BODY_BYTES = 64 * 1024 * 1024

BUILD_INFO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build-info.json"
)


def load_build_info() -> dict:
    try:
        with open(BUILD_INFO_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"name": "bwf-clip-api", "version": __version__, "builder": "source"}


class Handler(BaseHTTPRequestHandler):
    server_version = f"BwfClip/{__version__}"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- response helpers -------------------------------------------------

    def _send_json(
        self,
        status: int,
        payload: dict,
        *,
        close: bool = False,
        extra_headers: Optional[dict] = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(
        self,
        status: int,
        error: dict,
        *,
        close: bool = False,
        extra_headers: Optional[dict] = None,
    ) -> None:
        self._send_json(status, {"error": error}, close=close, extra_headers=extra_headers)

    def _declared_length(self) -> Optional[int]:
        """Content-Length of the current request, or None if absent/invalid."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _drain_body(self, length: int) -> None:
        """Read and discard up to *length* bytes so the connection stays sane."""
        remaining = length
        try:
            while remaining > 0:
                block = self.rfile.read(min(remaining, 1024 * 1024))
                if not block:
                    break
                remaining -= len(block)
        except (OSError, TimeoutError):
            pass

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/version":
            self._send_json(200, load_build_info())
        elif self.path == "/api/bwf/clip":
            self._send_error(
                405,
                {"code": "method_not_allowed", "message": "use POST with multipart/form-data"},
                extra_headers={"Allow": "POST"},
            )
        elif self.path == "/":
            self._send_json(
                200,
                {
                    "service": "bwf-clip-api",
                    "version": load_build_info().get("version", __version__),
                    "endpoints": {
                        "POST /api/bwf/clip": "clip a BWF/WAV file (multipart/form-data)",
                        "GET /healthz": "health check",
                        "GET /version": "build information",
                    },
                },
            )
        else:
            self._send_error(404, {"code": "not_found", "message": f"no such endpoint: {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/api/bwf/clip":
            declared = self._declared_length()
            close = declared is None or declared > HARD_MAX_BODY_BYTES
            if not close:
                self._drain_body(declared)
            self._send_error(
                404,
                {"code": "not_found", "message": f"no such endpoint: {self.path}"},
                close=close,
            )
            return

        content_type = self.headers.get("Content-Type", "")
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self._send_error(
                411,
                {"code": "length_required", "message": "Content-Length header is required"},
                close=True,
            )
            return
        try:
            length = int(content_length)
        except ValueError:
            self._send_error(
                400,
                {"code": "invalid_content_length", "message": "Content-Length is not an integer"},
                close=True,
            )
            return
        if length < 0 or length > HARD_MAX_BODY_BYTES:
            self._send_error(
                413,
                {
                    "code": "request_too_large",
                    "message": f"request body exceeds the {MAX_BODY_BYTES}-byte limit",
                },
                close=True,  # body intentionally not drained
            )
            return
        if length > MAX_BODY_BYTES:
            self._drain_body(length)  # bounded read; lets the client read our 413
            self._send_error(
                413,
                {
                    "code": "request_too_large",
                    "message": f"request body exceeds the {MAX_BODY_BYTES}-byte limit",
                },
            )
            return
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            body = b""
        if len(body) < length:
            self._send_error(
                400,
                {"code": "truncated_body", "message": "connection closed before the body arrived"},
                close=True,
            )
            return

        try:
            parts = parse_multipart(body, content_type)
            file_part = single_part(parts, "file")
            start_frame = parse_uint(single_part(parts, "startFrame"))
            frame_count = parse_uint(single_part(parts, "frameCount"))
        except MultipartError as exc:
            status = 415 if exc.code == "unsupported_media_type" else 400
            self._send_error(status, exc.to_dict())
            return

        wav = file_part.data
        if not wav:
            self._send_error(
                400,
                {"code": "empty_file", "message": "form field 'file' is empty", "field": "file"},
            )
            return
        if len(wav) > MAX_WAV_BYTES:
            self._send_error(
                413,
                {
                    "code": "file_too_large",
                    "message": f"WAV file is {len(wav)} bytes, limit is {MAX_WAV_BYTES}",
                    "field": "file",
                },
            )
            return

        try:
            result = clip_bwf(wav, start_frame, frame_count)
        except BwfError as exc:
            status = 413 if exc.code == "file_too_large" else 400
            self._send_error(status, exc.to_dict())
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(result.wav)))
        self.send_header("Content-Disposition", 'attachment; filename="clip.wav"')
        self.send_header("X-Time-Reference", str(result.time_reference))
        self.send_header("X-Frame-Count", str(result.frame_count))
        self.send_header("X-Audio-SHA256", result.audio_sha256)
        self.end_headers()
        try:
            self.wfile.write(result.wav)
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = make_server(host, port)
    print(f"bwf-clip-api {__version__} listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
