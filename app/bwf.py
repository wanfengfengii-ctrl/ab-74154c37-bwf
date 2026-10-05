"""Parsing and frame-accurate clipping of Broadcast Wave Format (BWF) files.

Only little-endian RIFF/WAVE containers are accepted, with integer PCM
samples of 16 or 24 bits, 1-8 channels, and exactly one ``fmt ``,
``bext`` and ``data`` chunk each.  Clipping never re-encodes audio: the
requested sample frames are copied byte-for-byte, the ``bext``
TimeReference is advanced by ``startFrame`` samples, and the RIFF/data
sizes plus pad bytes are rewritten according to RIFF alignment rules
(every chunk payload starts on an even offset; odd payloads are followed
by one zero pad byte that is not counted in the chunk size).

All client-detectable problems raise :class:`BwfError`, which carries a
machine-readable ``code`` plus optional ``field``/``chunk``/``offset``
locators so the API layer can return a precise 4xx response.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import List, Optional

MAX_WAV_BYTES = 16 * 1024 * 1024  # 16 MiB upload ceiling
UINT64_MAX = (1 << 64) - 1

# bext chunk layout (EBU Tech 3285): 256 description + 32 originator +
# 32 originator reference + 10 origination date + 8 origination time,
# then the 64-bit little-endian TimeReference at offset 338.
BEXT_TIME_REFERENCE_OFFSET = 338
BEXT_MIN_SIZE = BEXT_TIME_REFERENCE_OFFSET + 8  # 346

_FMT = b"fmt "
_BEXT = b"bext"
_DATA = b"data"


class BwfError(Exception):
    """A locatable, client-caused (4xx) rejection."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: Optional[str] = None,
        chunk: Optional[str] = None,
        offset: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.chunk = chunk
        self.offset = offset

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.field is not None:
            out["field"] = self.field
        if self.chunk is not None:
            out["chunk"] = self.chunk
        if self.offset is not None:
            out["offset"] = self.offset
        return out


@dataclass
class Chunk:
    cid: bytes
    size: int
    data_offset: int  # absolute offset of the payload within the source file


@dataclass
class BwfInfo:
    chunks: List[Chunk]
    fmt: Chunk
    bext: Chunk
    data: Chunk
    audio_format: int
    channels: int
    sample_rate: int
    byte_rate: int
    block_align: int
    bits_per_sample: int
    time_reference: int
    total_frames: int


@dataclass
class ClipResult:
    wav: bytes
    time_reference: int  # new bext TimeReference (clip start, samples since midnight)
    frame_count: int
    audio_sha256: str  # hex digest of the clipped PCM payload (data chunk bytes)
    total_frames: int  # frames available in the source
    sample_rate: int
    channels: int
    bits_per_sample: int


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def parse_bwf(buf: bytes) -> BwfInfo:
    """Validate *buf* as an acceptable BWF file and return its structure."""
    if len(buf) > MAX_WAV_BYTES:
        raise BwfError(
            "file_too_large",
            f"WAV file is {len(buf)} bytes, limit is {MAX_WAV_BYTES}",
            field="file",
        )
    if len(buf) < 12:
        raise BwfError(
            "truncated_riff",
            f"file is {len(buf)} bytes, smaller than the 12-byte RIFF header",
            field="file",
            offset=0,
        )
    riff_id = bytes(buf[0:4])
    if riff_id != b"RIFF":
        if riff_id in (b"RIFX", b"RF64", b"BW64"):
            raise BwfError(
                "unsupported_container",
                f"container {riff_id.decode('ascii')} is not little-endian RIFF",
                field="file",
                offset=0,
            )
        raise BwfError(
            "bad_riff_magic",
            f"expected 'RIFF' magic, found {riff_id!r}",
            field="file",
            offset=0,
        )
    riff_size = _u32(buf, 4)
    if buf[8:12] != b"WAVE":
        raise BwfError(
            "bad_wave_form",
            f"RIFF form type is {bytes(buf[8:12])!r}, expected 'WAVE'",
            field="file",
            offset=8,
        )
    riff_end = 8 + riff_size
    if riff_end > len(buf):
        raise BwfError(
            "truncated_riff",
            f"RIFF size field covers {riff_end} bytes but file has {len(buf)}",
            field="file",
            offset=4,
        )

    chunks: List[Chunk] = []
    pos = 12
    while pos < riff_end:
        remaining = riff_end - pos
        if remaining < 8:
            if remaining == 1:
                break  # single trailing pad byte after an odd-sized last chunk
            raise BwfError(
                "truncated_chunk_header",
                f"{remaining} dangling byte(s) after the last complete chunk",
                field="file",
                offset=pos,
            )
        cid = bytes(buf[pos : pos + 4])
        size = _u32(buf, pos + 4)
        data_off = pos + 8
        if data_off + size > riff_end:
            raise BwfError(
                "chunk_overrun",
                f"chunk {cid!r} at offset {pos} declares {size} bytes, "
                f"overrunning the RIFF boundary at {riff_end}",
                field="file",
                chunk=cid.decode("latin-1"),
                offset=pos,
            )
        chunks.append(Chunk(cid, size, data_off))
        pos = data_off + size + (size & 1)

    found = {}
    for cid, label in ((_FMT, "fmt "), (_BEXT, "bext"), (_DATA, "data")):
        matches = [c for c in chunks if c.cid == cid]
        if not matches:
            raise BwfError(
                "missing_chunk",
                f"required chunk '{label}' not found",
                field="file",
                chunk=label,
            )
        if len(matches) > 1:
            raise BwfError(
                "duplicate_chunk",
                f"chunk '{label}' appears {len(matches)} times, exactly one is allowed",
                field="file",
                chunk=label,
                offset=matches[1].data_offset - 8,
            )
        found[label] = matches[0]
    fmt, bext, data = found["fmt "], found["bext"], found["data"]

    if fmt.size < 16:
        raise BwfError(
            "fmt_too_small",
            f"fmt chunk is {fmt.size} bytes, PCM requires at least 16",
            field="file",
            chunk="fmt ",
            offset=fmt.data_offset - 8,
        )
    (audio_format, channels, sample_rate, byte_rate, block_align,
     bits_per_sample) = struct.unpack_from("<HHIIHH", buf, fmt.data_offset)
    if audio_format != 1:
        raise BwfError(
            "unsupported_format",
            f"audio format {audio_format} is not integer PCM (format 1)",
            field="file",
            chunk="fmt ",
        )
    if not 1 <= channels <= 8:
        raise BwfError(
            "unsupported_channels",
            f"channel count {channels} is outside the supported 1-8 range",
            field="file",
            chunk="fmt ",
        )
    if bits_per_sample not in (16, 24):
        raise BwfError(
            "unsupported_bit_depth",
            f"bit depth {bits_per_sample} is not supported, only 16 or 24",
            field="file",
            chunk="fmt ",
        )
    if sample_rate < 1:
        raise BwfError(
            "invalid_sample_rate",
            f"sample rate {sample_rate} is not positive",
            field="file",
            chunk="fmt ",
        )
    expected_align = channels * (bits_per_sample // 8)
    if block_align != expected_align:
        raise BwfError(
            "invalid_block_align",
            f"blockAlign {block_align} does not equal "
            f"channels*bytesPerSample ({expected_align}); frames are not decodable",
            field="file",
            chunk="fmt ",
        )

    if bext.size < BEXT_MIN_SIZE:
        raise BwfError(
            "bext_too_small",
            f"bext chunk is {bext.size} bytes, {BEXT_MIN_SIZE} are required "
            f"to hold the 64-bit TimeReference",
            field="file",
            chunk="bext",
            offset=bext.data_offset - 8,
        )
    time_reference = _u64(buf, bext.data_offset + BEXT_TIME_REFERENCE_OFFSET)

    if data.size % block_align != 0:
        raise BwfError(
            "non_integral_frames",
            f"data chunk holds {data.size} bytes, not a multiple of the "
            f"{block_align}-byte sample frame",
            field="file",
            chunk="data",
            offset=data.data_offset - 8,
        )
    total_frames = data.size // block_align

    return BwfInfo(
        chunks=chunks,
        fmt=fmt,
        bext=bext,
        data=data,
        audio_format=audio_format,
        channels=channels,
        sample_rate=sample_rate,
        byte_rate=byte_rate,
        block_align=block_align,
        bits_per_sample=bits_per_sample,
        time_reference=time_reference,
        total_frames=total_frames,
    )


def clip_bwf(buf: bytes, start_frame: int, frame_count: int) -> ClipResult:
    """Clip ``frame_count`` frames starting at ``start_frame`` from *buf*.

    The output preserves the sample rate, channel count and per-sample
    bytes, advances the bext TimeReference by ``start_frame`` and rewrites
    the data chunk, RIFF size and pad bytes.
    """
    info = parse_bwf(buf)

    if start_frame < 0:
        raise BwfError(
            "invalid_parameter",
            f"startFrame must be zero or positive, got {start_frame}",
            field="startFrame",
        )
    if frame_count < 1:
        raise BwfError(
            "invalid_parameter",
            f"frameCount must be a positive integer, got {frame_count}",
            field="frameCount",
        )
    if start_frame > UINT64_MAX:
        raise BwfError(
            "parameter_overflow",
            f"startFrame {start_frame} exceeds the 64-bit range",
            field="startFrame",
        )
    if frame_count > UINT64_MAX:
        raise BwfError(
            "parameter_overflow",
            f"frameCount {frame_count} exceeds the 64-bit range",
            field="frameCount",
        )

    end_frame = start_frame + frame_count
    if end_frame > info.total_frames:
        raise BwfError(
            "out_of_range",
            f"requested frames [{start_frame}, {end_frame}) exceed the "
            f"{info.total_frames} frames in the file",
            field="startFrame" if start_frame >= info.total_frames else "frameCount",
        )

    new_time_reference = info.time_reference + start_frame
    if new_time_reference > UINT64_MAX:
        raise BwfError(
            "time_reference_overflow",
            f"TimeReference {info.time_reference} + startFrame {start_frame} "
            f"overflows the 64-bit TimeReference field",
            field="startFrame",
            chunk="bext",
        )

    align = info.block_align
    audio = buf[
        info.data.data_offset + start_frame * align:
        info.data.data_offset + end_frame * align
    ]

    out = bytearray()
    out += b"RIFF\x00\x00\x00\x00WAVE"  # size patched below
    for chunk in info.chunks:
        if chunk.cid == _DATA:
            payload = audio
        elif chunk.cid == _BEXT:
            patched = bytearray(buf[chunk.data_offset:chunk.data_offset + chunk.size])
            struct.pack_into("<Q", patched, BEXT_TIME_REFERENCE_OFFSET, new_time_reference)
            payload = bytes(patched)
        else:
            payload = buf[chunk.data_offset:chunk.data_offset + chunk.size]
        out += chunk.cid
        out += struct.pack("<I", len(payload))
        out += payload
        if len(payload) & 1:
            out += b"\x00"  # RIFF word alignment: pad byte not counted in size
    struct.pack_into("<I", out, 4, len(out) - 8)

    return ClipResult(
        wav=bytes(out),
        time_reference=new_time_reference,
        frame_count=frame_count,
        audio_sha256=hashlib.sha256(audio).hexdigest(),
        total_frames=info.total_frames,
        sample_rate=info.sample_rate,
        channels=info.channels,
        bits_per_sample=info.bits_per_sample,
    )
