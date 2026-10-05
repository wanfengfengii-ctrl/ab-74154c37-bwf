"""Unit tests for app.bwf: parsing, validation, clipping and RIFF rewriting."""

from __future__ import annotations

import hashlib
import struct
import unittest

from app.bwf import (
    BEXT_TIME_REFERENCE_OFFSET,
    MAX_WAV_BYTES,
    UINT64_MAX,
    BwfError,
    clip_bwf,
    parse_bwf,
)
from tests.wavfactory import (
    make_bext_payload,
    make_bwf,
    make_fmt_payload,
    pcm_pattern,
)


def expect_error(testcase, code, func, *args):
    with testcase.assertRaises(BwfError) as ctx:
        func(*args)
    testcase.assertEqual(ctx.exception.code, code)
    return ctx.exception


class ParseTests(unittest.TestCase):
    def test_parse_valid_file(self):
        wav = make_bwf(channels=2, bits=16, rate=48000, frames=100, time_ref=42)
        info = parse_bwf(wav)
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.sample_rate, 48000)
        self.assertEqual(info.bits_per_sample, 16)
        self.assertEqual(info.block_align, 4)
        self.assertEqual(info.time_reference, 42)
        self.assertEqual(info.total_frames, 100)

    def test_reject_file_over_16mib(self):
        err = expect_error(
            self, "file_too_large", parse_bwf, b"\x00" * (MAX_WAV_BYTES + 1)
        )
        self.assertEqual(err.field, "file")

    def test_reject_tiny_file(self):
        expect_error(self, "truncated_riff", parse_bwf, b"RIFF")

    def test_reject_non_riff_magic(self):
        wav = make_bwf(riff_magic=b"NOPE")
        expect_error(self, "bad_riff_magic", parse_bwf, wav)

    def test_reject_rifx_big_endian(self):
        wav = make_bwf(riff_magic=b"RIFX")
        expect_error(self, "unsupported_container", parse_bwf, wav)

    def test_reject_rf64(self):
        wav = make_bwf(riff_magic=b"RF64")
        expect_error(self, "unsupported_container", parse_bwf, wav)

    def test_reject_non_wave_form(self):
        wav = make_bwf(form_type=b"AVI ")
        expect_error(self, "bad_wave_form", parse_bwf, wav)

    def test_reject_riff_size_beyond_file(self):
        wav = make_bwf(riff_size_delta=64)
        expect_error(self, "truncated_riff", parse_bwf, wav)

    def test_trailing_bytes_after_riff_are_ignored(self):
        wav = make_bwf(frames=8, trailing=b"GARBAGE")
        info = parse_bwf(wav)
        self.assertEqual(info.total_frames, 8)

    def test_reject_chunk_overrun(self):
        wav = bytearray(make_bwf(frames=4))
        # Corrupt the data chunk size so it overruns the RIFF boundary.
        idx = wav.find(b"data")
        struct.pack_into("<I", wav, idx + 4, 10_000_000)
        expect_error(self, "chunk_overrun", parse_bwf, bytes(wav))

    def test_reject_missing_fmt(self):
        wav = make_bwf(fmt_payload=None)
        # remove the fmt chunk entirely by rebuilding without it
        bext = make_bext_payload(0)
        data = pcm_pattern(16)
        body = b"WAVE" + b"bext" + struct.pack("<I", len(bext)) + bext
        body += b"data" + struct.pack("<I", len(data)) + data
        wav = b"RIFF" + struct.pack("<I", len(body)) + body
        expect_error(self, "missing_chunk", parse_bwf, wav)

    def test_reject_missing_bext(self):
        fmt = make_fmt_payload()
        data = pcm_pattern(16)
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        body += b"data" + struct.pack("<I", len(data)) + data
        wav = b"RIFF" + struct.pack("<I", len(body)) + body
        err = expect_error(self, "missing_chunk", parse_bwf, wav)
        self.assertEqual(err.chunk, "bext")

    def test_reject_missing_data(self):
        fmt = make_fmt_payload()
        bext = make_bext_payload(0)
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        body += b"bext" + struct.pack("<I", len(bext)) + bext
        wav = b"RIFF" + struct.pack("<I", len(body)) + body
        err = expect_error(self, "missing_chunk", parse_bwf, wav)
        self.assertEqual(err.chunk, "data")

    def test_reject_duplicate_data(self):
        wav = make_bwf(frames=8, pre_chunks=[(b"data", pcm_pattern(8))])
        err = expect_error(self, "duplicate_chunk", parse_bwf, wav)
        self.assertEqual(err.chunk, "data")

    def test_reject_duplicate_fmt(self):
        wav = make_bwf(frames=8, post_chunks=[(b"fmt ", make_fmt_payload())])
        expect_error(self, "duplicate_chunk", parse_bwf, wav)

    def test_reject_duplicate_bext(self):
        wav = make_bwf(frames=8, post_chunks=[(b"bext", make_bext_payload(0))])
        expect_error(self, "duplicate_chunk", parse_bwf, wav)

    def test_reject_fmt_too_small(self):
        wav = make_bwf(fmt_payload=b"\x01\x00\x02\x00")
        expect_error(self, "fmt_too_small", parse_bwf, wav)

    def test_reject_non_pcm_format(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(audio_format=3))
        expect_error(self, "unsupported_format", parse_bwf, wav)

    def test_reject_zero_channels(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(channels=0, block_align=0, byte_rate=0))
        expect_error(self, "unsupported_channels", parse_bwf, wav)

    def test_reject_nine_channels(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(channels=9))
        expect_error(self, "unsupported_channels", parse_bwf, wav)

    def test_accept_eight_channels(self):
        wav = make_bwf(channels=8, bits=24, frames=4)
        info = parse_bwf(wav)
        self.assertEqual(info.channels, 8)

    def test_reject_8bit(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(bits=8))
        expect_error(self, "unsupported_bit_depth", parse_bwf, wav)

    def test_reject_32bit(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(bits=32))
        expect_error(self, "unsupported_bit_depth", parse_bwf, wav)

    def test_reject_block_align_mismatch(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(block_align=99))
        expect_error(self, "invalid_block_align", parse_bwf, wav)

    def test_reject_bext_too_small(self):
        wav = make_bwf(bext_payload=make_bext_payload(0, size=100))
        expect_error(self, "bext_too_small", parse_bwf, wav)

    def test_reject_non_integral_frames(self):
        payload = pcm_pattern(4 * 10 + 1)  # 10 stereo frames plus one stray byte
        wav = make_bwf(channels=2, bits=16, data_payload=payload)
        err = expect_error(self, "non_integral_frames", parse_bwf, wav)
        self.assertEqual(err.chunk, "data")


class ClipTests(unittest.TestCase):
    def test_clip_copies_exact_sample_bytes(self):
        frames, start, count = 480, 100, 50
        wav = make_bwf(channels=2, bits=16, frames=frames, time_ref=1000)
        result = clip_bwf(wav, start, count)
        source = parse_bwf(wav)
        align = source.block_align
        expected = wav[
            source.data.data_offset + start * align:
            source.data.data_offset + (start + count) * align
        ]
        out = parse_bwf(result.wav)
        self.assertEqual(out.total_frames, count)
        self.assertEqual(
            result.wav[out.data.data_offset:out.data.data_offset + out.data.size],
            expected,
        )
        self.assertEqual(result.audio_sha256, hashlib.sha256(expected).hexdigest())

    def test_clip_preserves_format_fields(self):
        wav = make_bwf(channels=1, bits=24, rate=96000, frames=64, time_ref=7)
        result = clip_bwf(wav, 3, 20)
        out = parse_bwf(result.wav)
        self.assertEqual(out.sample_rate, 96000)
        self.assertEqual(out.channels, 1)
        self.assertEqual(out.bits_per_sample, 24)
        self.assertEqual(out.block_align, 3)

    def test_time_reference_advances_by_start_frame(self):
        wav = make_bwf(frames=100, time_ref=2**40)
        result = clip_bwf(wav, 25, 10)
        self.assertEqual(result.time_reference, 2**40 + 25)
        out = parse_bwf(result.wav)
        self.assertEqual(out.time_reference, 2**40 + 25)

    def test_time_reference_written_at_bext_offset_338(self):
        wav = make_bwf(frames=10, time_ref=5)
        result = clip_bwf(wav, 4, 2)
        out = parse_bwf(result.wav)
        raw = struct.unpack_from(
            "<Q", result.wav, out.bext.data_offset + BEXT_TIME_REFERENCE_OFFSET
        )[0]
        self.assertEqual(raw, 9)

    def test_odd_data_size_gets_pad_byte(self):
        # mono 24-bit -> 3-byte frames; 3 frames -> 9 bytes (odd)
        wav = make_bwf(channels=1, bits=24, frames=9)
        result = clip_bwf(wav, 0, 3)
        out = parse_bwf(result.wav)
        self.assertEqual(out.data.size, 9)
        # pad byte follows the odd payload and is not counted in the size
        pad_at = out.data.data_offset + 9
        self.assertEqual(result.wav[pad_at], 0)
        # RIFF size accounts for the pad byte
        self.assertEqual(struct.unpack_from("<I", result.wav, 4)[0], len(result.wav) - 8)

    def test_odd_sized_metadata_chunk_preserved_with_padding(self):
        odd_payload = b"INFO" + b"x" * 5  # 9 bytes
        wav = make_bwf(
            frames=32,
            time_ref=11,
            pre_chunks=[(b"LIST", odd_payload)],
            post_chunks=[(b"JUNK", b"abc")],
        )
        result = clip_bwf(wav, 2, 10)
        out = parse_bwf(result.wav)
        list_chunks = [c for c in out.chunks if c.cid == b"LIST"]
        junk_chunks = [c for c in out.chunks if c.cid == b"JUNK"]
        self.assertEqual(len(list_chunks), 1)
        self.assertEqual(len(junk_chunks), 1)
        for chunk, payload in ((list_chunks[0], odd_payload), (junk_chunks[0], b"abc")):
            self.assertEqual(chunk.size, len(payload))
            start = chunk.data_offset
            self.assertEqual(result.wav[start:start + len(payload)], payload)
            self.assertEqual(result.wav[start + len(payload)], 0)  # pad byte
        # every chunk payload starts on an even offset
        for chunk in out.chunks:
            self.assertEqual(chunk.data_offset % 2, 0)

    def test_output_is_reparseable_and_riff_size_consistent(self):
        wav = make_bwf(frames=500, time_ref=123, pre_chunks=[(b"LIST", b"abcd")])
        result = clip_bwf(wav, 100, 300)
        self.assertEqual(struct.unpack_from("<I", result.wav, 4)[0], len(result.wav) - 8)
        out = parse_bwf(result.wav)  # must not raise
        self.assertEqual(out.total_frames, 300)
        self.assertEqual(out.time_reference, 223)

    def test_clip_full_range(self):
        wav = make_bwf(frames=64, time_ref=0)
        result = clip_bwf(wav, 0, 64)
        self.assertEqual(result.frame_count, 64)
        self.assertEqual(parse_bwf(result.wav).total_frames, 64)

    def test_clip_last_frame(self):
        wav = make_bwf(frames=64, time_ref=100)
        result = clip_bwf(wav, 63, 1)
        self.assertEqual(result.time_reference, 163)

    def test_reject_negative_start(self):
        wav = make_bwf(frames=8)
        err = expect_error(self, "invalid_parameter", clip_bwf, wav, -1, 1)
        self.assertEqual(err.field, "startFrame")

    def test_reject_zero_frame_count(self):
        wav = make_bwf(frames=8)
        err = expect_error(self, "invalid_parameter", clip_bwf, wav, 0, 0)
        self.assertEqual(err.field, "frameCount")

    def test_reject_start_beyond_end(self):
        wav = make_bwf(frames=8)
        err = expect_error(self, "out_of_range", clip_bwf, wav, 8, 1)
        self.assertEqual(err.field, "startFrame")

    def test_reject_count_beyond_end(self):
        wav = make_bwf(frames=8)
        err = expect_error(self, "out_of_range", clip_bwf, wav, 6, 3)
        self.assertEqual(err.field, "frameCount")

    def test_reject_parameter_overflow(self):
        wav = make_bwf(frames=8)
        expect_error(self, "parameter_overflow", clip_bwf, wav, UINT64_MAX + 1, 1)
        expect_error(self, "parameter_overflow", clip_bwf, wav, 0, UINT64_MAX + 1)

    def test_reject_time_reference_overflow(self):
        wav = make_bwf(frames=8, time_ref=UINT64_MAX)
        err = expect_error(self, "time_reference_overflow", clip_bwf, wav, 1, 1)
        self.assertEqual(err.chunk, "bext")

    def test_time_reference_max_exact_is_allowed(self):
        wav = make_bwf(frames=8, time_ref=UINT64_MAX - 3)
        result = clip_bwf(wav, 3, 1)
        self.assertEqual(result.time_reference, UINT64_MAX)

    def test_error_carries_locators(self):
        wav = make_bwf(fmt_payload=make_fmt_payload(bits=32))
        err = expect_error(self, "unsupported_bit_depth", clip_bwf, wav, 0, 1)
        as_dict = err.to_dict()
        self.assertEqual(as_dict["chunk"], "fmt ")
        self.assertIn("code", as_dict)
        self.assertIn("message", as_dict)


if __name__ == "__main__":
    unittest.main()
