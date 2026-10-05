"""End-to-end API tests against an in-process server instance."""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import unittest

from app.bwf import MAX_WAV_BYTES, parse_bwf
from app.server import make_server
from tests.wavfactory import make_bwf

BOUNDARY = "----apiboundary0123456789"


def multipart_body(fields):
    """fields: list of (name, value_bytes, filename_or_None)."""
    out = bytearray()
    for name, value, filename in fields:
        out += f"--{BOUNDARY}\r\n".encode()
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += (disposition + "\r\n").encode()
        if filename is not None:
            out += b"Content-Type: audio/wav\r\n"
        out += b"\r\n"
        out += value
        out += b"\r\n"
    out += f"--{BOUNDARY}--\r\n".encode()
    return bytes(out)


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        result = (resp.status, dict(resp.getheaders()), payload)
        conn.close()
        return result

    def post_clip(self, fields, content_type=None):
        body = multipart_body(fields)
        ctype = content_type or f"multipart/form-data; boundary={BOUNDARY}"
        return self.request("POST", "/api/bwf/clip", body, {"Content-Type": ctype})

    def clip_fields(self, wav, start, count):
        return [
            ("file", wav, "input.wav"),
            ("startFrame", str(start).encode(), None),
            ("frameCount", str(count).encode(), None),
        ]

    @staticmethod
    def error_body(payload):
        return json.loads(payload)["error"]

    # -- happy path ---------------------------------------------------------

    def test_health_endpoint(self):
        status, headers, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["status"], "ok")

    def test_version_endpoint(self):
        status, headers, payload = self.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertIn("version", json.loads(payload))

    def test_successful_clip(self):
        wav = make_bwf(channels=2, bits=16, rate=48000, frames=1000, time_ref=777)
        status, headers, payload = self.post_clip(self.clip_fields(wav, 100, 250))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(headers["X-Time-Reference"], "877")
        self.assertEqual(headers["X-Frame-Count"], "250")
        self.assertEqual(int(headers["Content-Length"]), len(payload))

        out = parse_bwf(payload)  # response must be a parseable WAV again
        self.assertEqual(out.total_frames, 250)
        self.assertEqual(out.time_reference, 877)
        self.assertEqual(out.sample_rate, 48000)
        self.assertEqual(out.channels, 2)

        source = parse_bwf(wav)
        align = source.block_align
        expected_audio = wav[
            source.data.data_offset + 100 * align:
            source.data.data_offset + 350 * align
        ]
        self.assertEqual(
            payload[out.data.data_offset:out.data.data_offset + out.data.size],
            expected_audio,
        )
        self.assertEqual(
            headers["X-Audio-SHA256"], hashlib.sha256(expected_audio).hexdigest()
        )

    def test_successful_clip_24bit_odd_padding(self):
        wav = make_bwf(channels=1, bits=24, frames=101, time_ref=0)
        status, headers, payload = self.post_clip(self.clip_fields(wav, 0, 3))
        self.assertEqual(status, 200)
        out = parse_bwf(payload)
        self.assertEqual(out.data.size, 9)
        self.assertEqual(payload[out.data.data_offset + 9], 0)

    # -- client errors --------------------------------------------------------

    def test_missing_file_field(self):
        status, headers, payload = self.post_clip(
            [("startFrame", b"0", None), ("frameCount", b"1", None)]
        )
        self.assertEqual(status, 400)
        err = self.error_body(payload)
        self.assertEqual(err["code"], "missing_field")
        self.assertEqual(err["field"], "file")

    def test_missing_start_frame(self):
        wav = make_bwf(frames=8)
        status, headers, payload = self.post_clip(
            [("file", wav, "a.wav"), ("frameCount", b"1", None)]
        )
        self.assertEqual(status, 400)
        err = self.error_body(payload)
        self.assertEqual(err["code"], "missing_field")
        self.assertEqual(err["field"], "startFrame")

    def test_non_integer_frame_count(self):
        wav = make_bwf(frames=8)
        status, headers, payload = self.post_clip(
            self.clip_fields(wav, 0, 1)[:-1] + [("frameCount", b"1.5", None)]
        )
        self.assertEqual(status, 400)
        err = self.error_body(payload)
        self.assertEqual(err["code"], "invalid_parameter")
        self.assertEqual(err["field"], "frameCount")

    def test_negative_start_frame_rejected(self):
        wav = make_bwf(frames=8)
        status, headers, payload = self.post_clip(self.clip_fields(wav, -1, 1))
        self.assertEqual(status, 400)
        self.assertEqual(self.error_body(payload)["code"], "invalid_parameter")

    def test_zero_frame_count_rejected(self):
        wav = make_bwf(frames=8)
        status, headers, payload = self.post_clip(self.clip_fields(wav, 0, 0))
        self.assertEqual(status, 400)
        err = self.error_body(payload)
        self.assertEqual(err["code"], "invalid_parameter")
        self.assertEqual(err["field"], "frameCount")

    def test_out_of_range(self):
        wav = make_bwf(frames=10)
        status, headers, payload = self.post_clip(self.clip_fields(wav, 5, 100))
        self.assertEqual(status, 400)
        err = self.error_body(payload)
        self.assertEqual(err["code"], "out_of_range")
        self.assertEqual(err["field"], "frameCount")

    def test_malformed_wav(self):
        status, headers, payload = self.post_clip(
            self.clip_fields(b"this is not a wav file at all........", 0, 1)
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.error_body(payload)["code"], "bad_riff_magic")

    def test_error_responses_are_json_without_audio(self):
        wav = make_bwf(frames=4)
        status, headers, payload = self.post_clip(self.clip_fields(wav, 0, 99))
        self.assertEqual(status, 400)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertNotIn(b"RIFF", payload)  # no partial audio leaks into errors

    def test_wrong_content_type(self):
        status, headers, payload = self.request(
            "POST", "/api/bwf/clip", b"{}", {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.error_body(payload)["code"], "unsupported_media_type")

    def test_oversized_file(self):
        big = b"\x00" * (MAX_WAV_BYTES + 1)
        status, headers, payload = self.post_clip(self.clip_fields(big, 0, 1))
        self.assertEqual(status, 413)
        self.assertEqual(self.error_body(payload)["code"], "file_too_large")

    def test_oversized_request_body(self):
        body = b"x" * (MAX_WAV_BYTES + 2 * 1024 * 1024)
        status, headers, payload = self.request(
            "POST",
            "/api/bwf/clip",
            body,
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.error_body(payload)["code"], "request_too_large")

    def test_unknown_post_path(self):
        status, headers, payload = self.request(
            "POST", "/nope", b"abc", {"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(self.error_body(payload)["code"], "not_found")

    def test_get_on_clip_endpoint_is_405(self):
        status, headers, payload = self.request("GET", "/api/bwf/clip")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_malformed_multipart(self):
        status, headers, payload = self.request(
            "POST",
            "/api/bwf/clip",
            b"garbage without boundary",
            {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.error_body(payload)["code"], "malformed_multipart")

    def test_duplicate_field_rejected(self):
        wav = make_bwf(frames=8)
        fields = self.clip_fields(wav, 0, 1) + [("startFrame", b"0", None)]
        status, headers, payload = self.post_clip(fields)
        self.assertEqual(status, 400)
        err = self.error_body(payload)
        self.assertEqual(err["code"], "duplicate_field")
        self.assertEqual(err["field"], "startFrame")


if __name__ == "__main__":
    unittest.main()
