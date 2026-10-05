"""Deterministic BWF/WAV synthesizer shared by unit tests and the verifier."""

from __future__ import annotations

import struct
from typing import Sequence, Tuple


def pcm_pattern(nbytes: int) -> bytes:
    """Deterministic, position-dependent pseudo-audio bytes."""
    return bytes(((i * 131 + 7) & 0xFF) for i in range(nbytes))


def make_fmt_payload(
    channels: int = 2,
    rate: int = 48000,
    bits: int = 16,
    audio_format: int = 1,
    block_align: int | None = None,
    byte_rate: int | None = None,
    extra: int = 0,
) -> bytes:
    align = block_align if block_align is not None else channels * (bits // 8)
    brate = byte_rate if byte_rate is not None else rate * align
    payload = struct.pack("<HHIIHH", audio_format, channels, rate, brate, align, bits)
    return payload + b"\x00" * extra


def make_bext_payload(time_ref: int = 0, size: int = 602) -> bytes:
    """Build a bext payload; sizes below 346 truncate the canonical layout."""
    full = bytearray(max(size, 348))
    full[0:11] = b"verify clip"
    full[256:256 + 8] = b"verifier"
    full[288:288 + 4] = b"ref0"
    full[320:330] = b"2026-10-05"
    full[330:338] = b"00:00:00"
    struct.pack_into("<Q", full, 338, time_ref & 0xFFFFFFFFFFFFFFFF)
    struct.pack_into("<H", full, 346, 1)  # bext version
    return bytes(full[:size])


def encode_chunk(cid: bytes, payload: bytes) -> bytes:
    wire = cid + struct.pack("<I", len(payload)) + payload
    if len(payload) & 1:
        wire += b"\x00"
    return wire


def make_bwf(
    *,
    channels: int = 2,
    bits: int = 16,
    rate: int = 48000,
    frames: int = 480,
    time_ref: int = 0,
    fmt_payload: bytes | None = None,
    bext_payload: bytes | None = None,
    data_payload: bytes | None = None,
    pre_chunks: Sequence[Tuple[bytes, bytes]] = (),
    post_chunks: Sequence[Tuple[bytes, bytes]] = (),
    riff_magic: bytes = b"RIFF",
    form_type: bytes = b"WAVE",
    riff_size_delta: int = 0,
    trailing: bytes = b"",
) -> bytes:
    """Assemble a RIFF/WAVE file with one fmt, one bext and one data chunk.

    ``pre_chunks``/``post_chunks`` insert extra chunks around the bext/data
    pair; ``riff_size_delta`` and ``trailing`` allow crafting malformed or
    padded containers.
    """
    fmt_p = fmt_payload if fmt_payload is not None else make_fmt_payload(channels, rate, bits)
    bext_p = bext_payload if bext_payload is not None else make_bext_payload(time_ref)
    if data_payload is None:
        align = channels * (bits // 8)
        data_p = pcm_pattern(frames * align)
    else:
        data_p = data_payload

    body = form_type
    body += encode_chunk(b"fmt ", fmt_p)
    for cid, payload in pre_chunks:
        body += encode_chunk(cid, payload)
    body += encode_chunk(b"bext", bext_p)
    body += encode_chunk(b"data", data_p)
    for cid, payload in post_chunks:
        body += encode_chunk(cid, payload)

    riff_size = len(body) + riff_size_delta
    return riff_magic + struct.pack("<I", riff_size) + body + trailing
