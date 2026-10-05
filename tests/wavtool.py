"""Helpers to synthesize and inspect RIFF/WAVE BWF files in tests."""

import random
import struct

BEXT_TIME_REFERENCE_OFFSET = 338


def chunk(cid, payload, pad=True):
    out = cid + struct.pack("<I", len(payload)) + payload
    if pad and len(payload) % 2:
        out += b"\x00"
    return out


def riff_wrap(body, riff_size=None):
    if riff_size is None:
        riff_size = len(body)
    return b"RIFF" + struct.pack("<I", riff_size) + body


def make_fmt(channels=2, bits=16, rate=48000, tag=1, byte_rate=None, block_align=None):
    align = block_align if block_align is not None else channels * (bits // 8)
    brate = byte_rate if byte_rate is not None else rate * align
    return struct.pack("<HHIIHH", tag, channels, rate, brate, align, bits)


def make_bext(time_ref=0, size=602, description=b"field-recording archive"):
    payload = bytearray(size)
    payload[:min(len(description), 256)] = description[:256]
    struct.pack_into("<Q", payload, BEXT_TIME_REFERENCE_OFFSET, time_ref)
    if size >= 348:
        struct.pack_into("<H", payload, 346, 1)  # bext version
    return bytes(payload)


def make_bwf(*, frames=1000, channels=2, bits=16, rate=48000, time_ref=0,
             seed=1234, extra_chunks=(), data_payload=None, fmt_payload=None,
             bext_payload=None):
    """Build a well-formed little-endian RIFF/WAVE file with fmt/bext/data."""
    if fmt_payload is None:
        fmt_payload = make_fmt(channels, bits, rate)
    if bext_payload is None:
        bext_payload = make_bext(time_ref)
    if data_payload is None:
        data_payload = random.Random(seed).randbytes(frames * channels * (bits // 8))
    parts = [chunk(b"fmt ", fmt_payload), chunk(b"bext", bext_payload)]
    for cid, payload in extra_chunks:
        parts.append(chunk(cid, payload))
    parts.append(chunk(b"data", data_payload))
    return riff_wrap(b"WAVE" + b"".join(parts))


def parse_bwf(buf):
    """Strict re-parser used to validate clip outputs; returns {cid: payload}."""
    assert buf[0:4] == b"RIFF", "missing RIFF tag"
    assert buf[8:12] == b"WAVE", "missing WAVE form type"
    (riff_size,) = struct.unpack_from("<I", buf, 4)
    assert riff_size == len(buf) - 8, "RIFF size does not match file length"
    chunks = {}
    pos = 12
    while pos < len(buf):
        cid = buf[pos:pos + 4]
        (size,) = struct.unpack_from("<I", buf, pos + 4)
        payload = buf[pos + 8:pos + 8 + size]
        assert len(payload) == size, f"chunk {cid!r} truncated"
        assert cid not in chunks, f"duplicate chunk {cid!r}"
        chunks[cid] = payload
        pos += 8 + size + (size & 1)
    assert pos == len(buf), "chunks do not tile the file exactly"
    return chunks


def time_reference_of(bext_payload):
    return struct.unpack_from("<Q", bext_payload, BEXT_TIME_REFERENCE_OFFSET)[0]
