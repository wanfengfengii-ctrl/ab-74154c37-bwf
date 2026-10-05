"""One-shot verification service.

Runs three steps and reports through the process exit code:
  1. unit/integration tests of the code as packaged in this image
  2. image build check: build-info.json baked by the Dockerfile must exist
     and match what the running app container reports via /version
  3. smoke test: a valid clip request against the app service, validating
     status, headers, re-parseability of the WAV and byte-exact audio

Usage: python verify/verify.py   (APP_URL env var selects the app, default
http://127.0.0.1:8080)
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bwf import parse_bwf  # noqa: E402
from tests.wavfactory import make_bwf  # noqa: E402

APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8080").rstrip("/")
APP_TIMEOUT = float(os.environ.get("APP_TIMEOUT", "10"))
WAIT_SECONDS = float(os.environ.get("APP_WAIT_SECONDS", "60"))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not condition else ""), flush=True)
    if not condition:
        FAILURES.append(name)
    return condition


def step_unit_tests() -> None:
    print("== step 1/3: code tests (unittest) ==", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"],
        cwd=ROOT,
    )
    check("unit tests pass", proc.returncode == 0, f"exit code {proc.returncode}")


def _get_json(path: str):
    with urllib.request.urlopen(APP_URL + path, timeout=APP_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read())


def step_build_check() -> None:
    print("== step 2/3: image build check ==", flush=True)
    info_path = ROOT / "build-info.json"
    if not check(
        "build-info.json baked into image",
        info_path.is_file(),
        f"{info_path} missing; image was not built from the project Dockerfile",
    ):
        return
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        check("build-info.json is valid JSON", False, str(exc))
        return
    check(
        "build-info schema",
        all(info.get(k) for k in ("name", "version", "builder")),
        f"unexpected content: {info!r}",
    )
    try:
        status, remote = _get_json("/version")
    except Exception as exc:  # noqa: BLE001 - report any failure as a failed check
        check("app /version reachable", False, str(exc))
        return
    check("app /version responds 200", status == 200, f"status={status}")
    check(
        "app image matches verify image build",
        remote.get("name") == info.get("name") and remote.get("version") == info.get("version"),
        f"app reports {remote!r}, image carries {info!r}",
    )
    try:
        status, health = _get_json("/healthz")
    except Exception as exc:  # noqa: BLE001
        check("app /healthz reachable", False, str(exc))
        return
    check("app healthy", status == 200 and health.get("status") == "ok", f"status={status} body={health!r}")


def _multipart(fields, boundary="----verify-boundary-7f3a"):
    out = bytearray()
    for name, value, filename in fields:
        out += f"--{boundary}\r\n".encode()
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        out += (disposition + "\r\n").encode()
        if filename:
            out += b"Content-Type: audio/wav\r\n"
        out += b"\r\n" + value + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def _post_clip(fields):
    body = _multipart(fields)
    request = urllib.request.Request(
        APP_URL + "/api/bwf/clip",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=----verify-boundary-7f3a"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=APP_TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _wait_for_app() -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            status, _ = _get_json("/healthz")
            if status == 200:
                return True
        except Exception:  # noqa: BLE001 - retry until the deadline
            pass
        time.sleep(1)
    return False


def step_smoke() -> None:
    print("== step 3/3: valid-clip API smoke ==", flush=True)
    if not check("app becomes healthy", _wait_for_app(), f"no health from {APP_URL} within {WAIT_SECONDS}s"):
        return

    channels, bits, rate, frames = 2, 16, 48000, 2000
    time_ref, start, count = 1_234_567_890, 120, 640
    wav = make_bwf(
        channels=channels,
        bits=bits,
        rate=rate,
        frames=frames,
        time_ref=time_ref,
        pre_chunks=[(b"LIST", b"INFO" + b"x" * 5)],  # odd-sized metadata chunk
    )
    status, headers, payload = _post_clip(
        [
            ("file", wav, "field-recording.wav"),
            ("startFrame", str(start).encode(), None),
            ("frameCount", str(count).encode(), None),
        ]
    )
    if not check("clip responds 200", status == 200, f"status={status} body={payload[:300]!r}"):
        return
    check("content type is audio/wav", headers.get("Content-Type") == "audio/wav", headers.get("Content-Type", ""))
    check(
        "X-Time-Reference advanced by startFrame",
        headers.get("X-Time-Reference") == str(time_ref + start),
        f"got {headers.get('X-Time-Reference')}",
    )
    check("X-Frame-Count matches request", headers.get("X-Frame-Count") == str(count), f"got {headers.get('X-Frame-Count')}")

    align = channels * (bits // 8)
    source = parse_bwf(wav)
    expected_audio = wav[
        source.data.data_offset + start * align:
        source.data.data_offset + (start + count) * align
    ]
    check(
        "X-Audio-SHA256 matches clipped PCM",
        headers.get("X-Audio-SHA256") == hashlib.sha256(expected_audio).hexdigest(),
        f"got {headers.get('X-Audio-SHA256')}",
    )

    try:
        out = parse_bwf(payload)
    except Exception as exc:  # noqa: BLE001
        check("response WAV re-parses", False, str(exc))
        return
    check("response WAV re-parses", True)
    check("output frame count", out.total_frames == count, f"got {out.total_frames}")
    check("output time reference", out.time_reference == time_ref + start, f"got {out.time_reference}")
    check(
        "format preserved",
        (out.sample_rate, out.channels, out.bits_per_sample) == (rate, channels, bits),
        f"got {(out.sample_rate, out.channels, out.bits_per_sample)}",
    )
    check(
        "audio bytes identical to source slice",
        payload[out.data.data_offset:out.data.data_offset + out.data.size] == expected_audio,
    )
    check("metadata chunk preserved", any(c.cid == b"LIST" for c in out.chunks))

    # negative smoke: out-of-range must be a locatable 4xx without audio
    status, headers, payload = _post_clip(
        [
            ("file", wav, "field-recording.wav"),
            ("startFrame", b"1990", None),
            ("frameCount", b"100", None),
        ]
    )
    ok = status == 400
    code = None
    if ok:
        try:
            code = json.loads(payload)["error"]["code"]
        except (ValueError, KeyError):
            ok = False
    check("out-of-range rejected with 400", ok, f"status={status} body={payload[:200]!r}")
    check("out-of-range error code", code == "out_of_range", f"code={code!r}")
    check("no partial audio in error response", b"RIFF" not in payload)


def main() -> int:
    print(f"verify: app under test is {APP_URL}", flush=True)
    step_unit_tests()
    step_build_check()
    step_smoke()
    if FAILURES:
        print(f"verify: FAILED ({len(FAILURES)} check(s)): {', '.join(FAILURES)}", flush=True)
        return 1
    print("verify: ALL CHECKS PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
