"""End-to-end smoke test: valid clip against the running app service."""

import hashlib
import os
import struct
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
import wavtool  # noqa: E402

APP_URL = os.environ.get("APP_URL", "http://app:8000")

FRAMES, START, COUNT = 2000, 120, 640
CHANNELS, BITS, RATE = 2, 24, 48000
TIME_REF = 9876543210
ALIGN = CHANNELS * (BITS // 8)

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""),
          flush=True)
    if not cond:
        FAILURES.append(name)


def wait_ready():
    for _ in range(60):
        try:
            if requests.get(f"{APP_URL}/health", timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def main():
    check("app becomes healthy", wait_ready())
    if FAILURES:
        return 1

    audio = bytes((i * 31 + 7) % 256 for i in range(FRAMES * ALIGN))
    wav = wavtool.make_bwf(frames=FRAMES, channels=CHANNELS, bits=BITS, rate=RATE,
                           time_ref=TIME_REF, data_payload=audio,
                           extra_chunks=[(b"LIST", b"odd")])  # odd-sized unknown chunk

    res = requests.post(
        f"{APP_URL}/api/bwf/clip",
        files={"file": ("field.wav", wav, "audio/wav")},
        data={"startFrame": str(START), "frameCount": str(COUNT)},
        timeout=30,
    )
    check("clip returns 200", res.status_code == 200,
          f"got {res.status_code}: {res.text[:200]}")
    if res.status_code != 200:
        return 1

    expected_audio = audio[START * ALIGN:(START + COUNT) * ALIGN]
    check("content type is audio/wav",
          res.headers.get("Content-Type", "").startswith("audio/wav"),
          res.headers.get("Content-Type", ""))
    check("X-Time-Reference header",
          res.headers.get("X-Time-Reference") == str(TIME_REF + START),
          res.headers.get("X-Time-Reference", "<missing>"))
    check("X-Frame-Count header",
          res.headers.get("X-Frame-Count") == str(COUNT),
          res.headers.get("X-Frame-Count", "<missing>"))
    check("X-Audio-SHA256 header",
          res.headers.get("X-Audio-SHA256") == hashlib.sha256(expected_audio).hexdigest())

    try:
        chunks = wavtool.parse_bwf(res.content)
        fmt = chunks[b"fmt "]
        got = (struct.unpack_from("<H", fmt, 2)[0],
               struct.unpack_from("<I", fmt, 4)[0],
               struct.unpack_from("<H", fmt, 14)[0])
        check("output re-parses as RIFF/WAVE", True)
        check("sample rate/channels/bit depth preserved",
              got == (CHANNELS, RATE, BITS), str(got))
        check("audio is the exact frame slice", chunks[b"data"] == expected_audio)
        check("bext TimeReference advanced",
              wavtool.time_reference_of(chunks[b"bext"]) == TIME_REF + START)
        check("unknown chunk preserved", chunks.get(b"LIST") == b"odd")
    except (AssertionError, KeyError, struct.error) as exc:
        check("output re-parses as RIFF/WAVE", False, str(exc))

    bad = requests.post(
        f"{APP_URL}/api/bwf/clip",
        files={"file": ("field.wav", wav, "audio/wav")},
        data={"startFrame": str(FRAMES), "frameCount": "1"},
        timeout=30,
    )
    check("out-of-range request is 4xx", 400 <= bad.status_code < 500,
          str(bad.status_code))
    check("out-of-range error is locatable JSON",
          "error" in bad.json() and "code" in bad.json()["error"]
          if bad.headers.get("Content-Type", "").startswith("application/json")
          else False)
    check("out-of-range returns no audio", not bad.content.startswith(b"RIFF"))

    if FAILURES:
        print("SMOKE FAILED: " + ", ".join(FAILURES), flush=True)
        return 1
    print("SMOKE OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
