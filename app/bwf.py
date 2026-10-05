"""Broadcast Wave Format (BWF) clipping core.

Parses a little-endian RIFF/WAVE file carrying PCM 16/24-bit audio plus a
``bext`` metadata chunk, and produces a clipped copy whose bext
TimeReference is advanced by the clip start frame.  The output rewrites the
``data`` chunk, the RIFF length and all chunk padding according to RIFF
alignment rules while every other chunk (and the per-sample audio bytes) is
carried over untouched.

Every validation failure raises :class:`BWFError` carrying a stable
machine-readable ``code`` plus the chunk id and/or byte offset that
pinpoints the problem, so the API layer can return locatable 4xx responses.
Clipping is all-or-nothing: the output is assembled in memory only after
every check has passed, so a failed request can never yield partial audio.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # 16 MiB hard upload limit

_RIFF_HEADER_SIZE = 12
_CHUNK_HEADER_SIZE = 8

#: Byte offset of the 64-bit TimeReference inside a bext chunk payload
#: (EBU Tech 3285: 256 description + 32 originator + 32 originator
#:  reference + 10 date + 8 time = 338).
BEXT_TIME_REFERENCE_OFFSET = 338
BEXT_MIN_SIZE = BEXT_TIME_REFERENCE_OFFSET + 8

_UINT64_MAX = (1 << 64) - 1


class BWFError(Exception):
    """Validation failure with a locatable position inside the upload."""

    def __init__(self, code, message, *, chunk=None, offset=None, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.chunk = chunk
        self.offset = offset
        self.status = status

    def payload(self):
        error = {"code": self.code, "message": self.message}
        if self.chunk is not None:
            error["chunk"] = self.chunk
        if self.offset is not None:
            error["offset"] = self.offset
        return {"error": error}


@dataclass(frozen=True)
class Chunk:
    cid: bytes          # 4-byte chunk identifier
    header_offset: int  # offset of the 8-byte chunk header in the source
    size: int           # payload size in bytes (pad byte excluded)
    data_offset: int    # offset of the payload in the source


@dataclass(frozen=True)
class FormatInfo:
    channels: int
    sample_rate: int
    bits_per_sample: int
    block_align: int


@dataclass(frozen=True)
class ClipResult:
    data: bytes          # complete rewritten RIFF/WAVE file
    time_reference: int  # bext TimeReference of the first output frame
    frame_count: int     # number of sample frames in the output
    audio_sha256: str    # hex digest over the output data-chunk payload


def _u16(buf, off):
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf, off):
    return struct.unpack_from("<Q", buf, off)[0]


def _label(cid):
    return cid.decode("latin-1")


def _parse_chunks(buf):
    """Validate the RIFF/WAVE envelope and return its chunk directory."""
    if len(buf) < _RIFF_HEADER_SIZE:
        raise BWFError(
            "truncated_header",
            f"file is {len(buf)} bytes, shorter than the 12-byte RIFF/WAVE header",
            offset=0,
        )
    tag = bytes(buf[0:4])
    if tag != b"RIFF":
        raise BWFError(
            "unsupported_container",
            f"expected a little-endian 'RIFF' container, found {tag!r} "
            "(RIFX/RF64 and other variants are not accepted)",
            offset=0,
        )
    riff_size = _u32(buf, 4)
    if buf[8:12] != b"WAVE":
        raise BWFError(
            "not_wave",
            f"RIFF form type must be 'WAVE', found {bytes(buf[8:12])!r}",
            offset=8,
        )
    riff_end = _CHUNK_HEADER_SIZE + riff_size
    if riff_end > len(buf):
        raise BWFError(
            "truncated_riff",
            f"RIFF size field declares {riff_end} bytes but only {len(buf)} were uploaded",
            offset=4,
        )
    chunks = []
    pos = _RIFF_HEADER_SIZE
    while pos < riff_end:
        if pos + _CHUNK_HEADER_SIZE > riff_end:
            raise BWFError(
                "truncated_chunk_header",
                "chunk header extends past the RIFF boundary",
                offset=pos,
            )
        cid = bytes(buf[pos:pos + 4])
        size = _u32(buf, pos + 4)
        data_offset = pos + _CHUNK_HEADER_SIZE
        end = data_offset + size + (size & 1)  # payload + RIFF word-alignment pad
        if end > riff_end:
            raise BWFError(
                "truncated_chunk",
                f"chunk '{_label(cid)}' (declared {size} bytes plus pad) "
                "extends past the RIFF boundary",
                chunk=_label(cid),
                offset=pos,
            )
        chunks.append(Chunk(cid=cid, header_offset=pos, size=size, data_offset=data_offset))
        pos = end
    return chunks


def _find_unique(chunks, cid):
    label = _label(cid)
    matches = [c for c in chunks if c.cid == cid]
    if not matches:
        raise BWFError(
            "missing_chunk",
            f"required chunk '{label}' is absent",
            chunk=label,
        )
    if len(matches) > 1:
        raise BWFError(
            "duplicate_chunk",
            f"chunk '{label}' appears {len(matches)} times, exactly one is allowed",
            chunk=label,
            offset=matches[1].header_offset,
        )
    return matches[0]


def _parse_format(buf, fmt):
    label = "fmt "
    if fmt.size < 16:
        raise BWFError(
            "truncated_fmt",
            f"fmt chunk holds {fmt.size} bytes, PCM format needs at least 16",
            chunk=label,
            offset=fmt.header_offset,
        )
    base = fmt.data_offset
    audio_format = _u16(buf, base)
    channels = _u16(buf, base + 2)
    sample_rate = _u32(buf, base + 4)
    byte_rate = _u32(buf, base + 8)
    block_align = _u16(buf, base + 12)
    bits = _u16(buf, base + 14)
    if audio_format != 1:
        raise BWFError(
            "unsupported_encoding",
            f"only uncompressed PCM (format tag 1) is accepted, got tag {audio_format}",
            chunk=label,
            offset=base,
        )
    if not 1 <= channels <= 8:
        raise BWFError(
            "unsupported_channels",
            f"channel count must be between 1 and 8, got {channels}",
            chunk=label,
            offset=base + 2,
        )
    if bits not in (16, 24):
        raise BWFError(
            "unsupported_bit_depth",
            f"only 16-bit and 24-bit PCM are accepted, got {bits}-bit",
            chunk=label,
            offset=base + 14,
        )
    expected_align = channels * (bits // 8)
    if block_align != expected_align:
        raise BWFError(
            "bad_block_align",
            f"block align is {block_align}, expected {expected_align} "
            f"({channels} channels x {bits // 8} bytes)",
            chunk=label,
            offset=base + 12,
        )
    if sample_rate == 0:
        raise BWFError(
            "bad_sample_rate",
            "sample rate must be positive",
            chunk=label,
            offset=base + 4,
        )
    if byte_rate != sample_rate * block_align:
        raise BWFError(
            "bad_byte_rate",
            f"byte rate is {byte_rate}, expected {sample_rate * block_align} "
            f"(sample rate {sample_rate} x block align {block_align})",
            chunk=label,
            offset=base + 8,
        )
    return FormatInfo(channels=channels, sample_rate=sample_rate,
                      bits_per_sample=bits, block_align=block_align)


def clip_bwf(source, start_frame, frame_count):
    """Clip ``source`` to ``frame_count`` frames starting at ``start_frame``.

    Returns a :class:`ClipResult`; raises :class:`BWFError` before touching
    any output whenever the input or the requested range is invalid.
    """
    if start_frame < 0:
        raise BWFError(
            "bad_start_frame",
            f"startFrame must be zero or positive, got {start_frame}",
            status=422,
        )
    if frame_count < 1:
        raise BWFError(
            "bad_frame_count",
            f"frameCount must be a positive integer, got {frame_count}",
            status=422,
        )

    chunks = _parse_chunks(source)
    fmt = _parse_format(source, _find_unique(chunks, b"fmt "))
    bext = _find_unique(chunks, b"bext")
    data = _find_unique(chunks, b"data")

    if bext.size < BEXT_MIN_SIZE:
        raise BWFError(
            "truncated_bext",
            f"bext chunk holds {bext.size} bytes, at least {BEXT_MIN_SIZE} are "
            "required to reach the 64-bit TimeReference field",
            chunk="bext",
            offset=bext.header_offset,
        )

    if data.size % fmt.block_align:
        raise BWFError(
            "non_integral_frames",
            f"data chunk holds {data.size} bytes, which is not a whole number "
            f"of {fmt.block_align}-byte sample frames",
            chunk="data",
            offset=data.header_offset,
        )
    total_frames = data.size // fmt.block_align

    end_frame = start_frame + frame_count
    if end_frame > total_frames:
        raise BWFError(
            "range_out_of_bounds",
            f"requested frames [{start_frame}, {end_frame}) exceed the "
            f"{total_frames} frames present in the data chunk",
            chunk="data",
            offset=data.header_offset,
            status=422,
        )

    time_ref_offset = bext.data_offset + BEXT_TIME_REFERENCE_OFFSET
    time_reference = _u64(source, time_ref_offset) + start_frame
    if time_reference > _UINT64_MAX:
        raise BWFError(
            "time_reference_overflow",
            "advancing the 64-bit bext TimeReference by startFrame overflows",
            chunk="bext",
            offset=time_ref_offset,
            status=422,
        )

    first_byte = start_frame * fmt.block_align
    audio = source[data.data_offset + first_byte:
                   data.data_offset + first_byte + frame_count * fmt.block_align]
    digest = hashlib.sha256(audio).hexdigest()

    out = bytearray()
    out += b"RIFF\x00\x00\x00\x00WAVE"  # RIFF size patched after assembly
    for chunk in chunks:
        if chunk.cid == b"bext":
            payload = bytearray(source[chunk.data_offset:chunk.data_offset + chunk.size])
            struct.pack_into("<Q", payload, BEXT_TIME_REFERENCE_OFFSET, time_reference)
            payload = bytes(payload)
        elif chunk.cid == b"data":
            payload = audio
        else:
            payload = source[chunk.data_offset:chunk.data_offset + chunk.size]
        out += chunk.cid
        out += struct.pack("<I", len(payload))
        out += payload
        if len(payload) & 1:
            out += b"\x00"  # RIFF word-alignment pad
    struct.pack_into("<I", out, 4, len(out) - _CHUNK_HEADER_SIZE)

    return ClipResult(data=bytes(out), time_reference=time_reference,
                      frame_count=frame_count, audio_sha256=digest)
