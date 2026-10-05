import hashlib

import wavtool
from fastapi.testclient import TestClient

from app.bwf import MAX_UPLOAD_BYTES
from app.main import app

client = TestClient(app)


def post_clip(wav, start="0", count="1", **kwargs):
    return client.post(
        "/api/bwf/clip",
        files={"file": ("field.wav", wav, "audio/wav")},
        data={"startFrame": start, "frameCount": count},
        **kwargs,
    )


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_successful_clip_headers_and_body():
    align = 2 * 3  # 2 channels x 24-bit
    audio = bytes((i * 7 + 1) % 256 for i in range(500 * align))
    wav = wavtool.make_bwf(frames=500, channels=2, bits=24, rate=44100,
                           time_ref=123456789, data_payload=audio)

    res = post_clip(wav, start="120", count="200")

    assert res.status_code == 200, res.text
    assert res.headers["Content-Type"] == "audio/wav"
    assert res.headers["X-Time-Reference"] == str(123456789 + 120)
    assert res.headers["X-Frame-Count"] == "200"
    expected = audio[120 * align:320 * align]
    assert res.headers["X-Audio-SHA256"] == hashlib.sha256(expected).hexdigest()
    chunks = wavtool.parse_bwf(res.content)  # response must re-parse
    assert chunks[b"data"] == expected
    assert wavtool.time_reference_of(chunks[b"bext"]) == 123456789 + 120


def test_file_at_size_limit_accepted():
    overhead = 12 + (8 + 16) + (8 + 602) + 8  # header + fmt + bext + data header
    payload_len = MAX_UPLOAD_BYTES - overhead
    payload_len -= payload_len % 4  # keep whole 2ch/16-bit frames
    wav = wavtool.make_bwf(data_payload=b"\x00" * payload_len)
    assert len(wav) <= MAX_UPLOAD_BYTES
    res = post_clip(wav, start="0", count="1")
    assert res.status_code == 200


def test_file_over_size_limit_rejected():
    wav = wavtool.make_bwf(data_payload=b"\x00" * MAX_UPLOAD_BYTES)
    res = post_clip(wav)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "file_too_large"


def test_corrupt_file_is_locatable_400():
    res = post_clip(b"not a wave file at all")
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "unsupported_container"
    assert "offset" in error
    assert res.headers["Content-Type"].startswith("application/json")


def test_tiny_file_is_locatable_400():
    res = post_clip(b"RIFF")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "truncated_header"


def test_out_of_range_is_locatable_422_without_audio():
    wav = wavtool.make_bwf(frames=10)
    res = post_clip(wav, start="9", count="5")
    assert res.status_code == 422
    error = res.json()["error"]
    assert error["code"] == "range_out_of_bounds"
    assert error["chunk"] == "data"
    assert not res.content.startswith(b"RIFF"), "no partial audio may leak"


def test_time_reference_overflow_is_422():
    wav = wavtool.make_bwf(frames=4, time_ref=(1 << 64) - 1)
    res = post_clip(wav, start="1", count="1")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "time_reference_overflow"


def test_non_integral_data_is_400():
    wav = wavtool.make_bwf(data_payload=b"\x00" * 10)
    res = post_clip(wav)
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "non_integral_frames"


def test_invalid_params_are_locatable_422():
    wav = wavtool.make_bwf(frames=10)
    for data in ({"startFrame": "-1", "frameCount": "1"},
                 {"startFrame": "abc", "frameCount": "1"},
                 {"startFrame": "0", "frameCount": "0"},
                 {"startFrame": "0", "frameCount": "1.5"}):
        res = client.post("/api/bwf/clip",
                          files={"file": ("f.wav", wav, "audio/wav")}, data=data)
        assert res.status_code == 422, data
        assert "detail" in res.json()


def test_missing_file_field_is_422():
    res = client.post("/api/bwf/clip",
                      data={"startFrame": "0", "frameCount": "1"})
    assert res.status_code == 422
