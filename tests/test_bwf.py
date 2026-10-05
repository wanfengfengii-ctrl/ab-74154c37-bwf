import hashlib
import struct

import pytest
import wavtool

from app.bwf import BWFError, clip_bwf


def expect_error(code, wav, start=0, count=1):
    with pytest.raises(BWFError) as excinfo:
        clip_bwf(wav, start, count)
    assert excinfo.value.code == code
    return excinfo.value


class TestSuccessfulClips:
    def test_middle_clip_preserves_audio_and_shifts_time_reference(self):
        align = 2 * 2  # 2 channels x 16-bit
        audio = bytes((i * 13 + 5) % 256 for i in range(1000 * align))
        wav = wavtool.make_bwf(frames=1000, channels=2, bits=16, rate=48000,
                               time_ref=5000, data_payload=audio)

        res = clip_bwf(wav, 100, 300)

        assert res.frame_count == 300
        assert res.time_reference == 5100
        expected = audio[100 * align:400 * align]
        assert res.audio_sha256 == hashlib.sha256(expected).hexdigest()

        chunks = wavtool.parse_bwf(res.data)  # output must re-parse cleanly
        assert chunks[b"data"] == expected
        fmt = chunks[b"fmt "]
        assert struct.unpack_from("<H", fmt, 2)[0] == 2       # channels
        assert struct.unpack_from("<I", fmt, 4)[0] == 48000   # sample rate
        assert struct.unpack_from("<H", fmt, 14)[0] == 16     # bits
        assert wavtool.time_reference_of(chunks[b"bext"]) == 5100

    def test_full_range_clip(self):
        wav = wavtool.make_bwf(frames=64, time_ref=7)
        res = clip_bwf(wav, 0, 64)
        assert res.frame_count == 64
        assert res.time_reference == 7
        assert len(wavtool.parse_bwf(res.data)[b"data"]) == 64 * 4

    def test_last_frame_clip(self):
        wav = wavtool.make_bwf(frames=10, time_ref=100)
        res = clip_bwf(wav, 9, 1)
        assert res.frame_count == 1
        assert res.time_reference == 109

    def test_odd_data_size_gets_alignment_pad(self):
        # mono 24-bit -> 3 bytes per frame; 5 frames -> odd 15-byte data chunk
        audio = bytes(range(1, 16))
        wav = wavtool.make_bwf(channels=1, bits=24, data_payload=audio)
        res = clip_bwf(wav, 1, 3)  # 9-byte (odd) output data chunk
        chunks = wavtool.parse_bwf(res.data)
        assert chunks[b"data"] == audio[3:12]
        assert len(res.data) % 2 == 0, "file must stay word-aligned"

    def test_unknown_and_odd_sized_chunks_are_preserved(self):
        wav = wavtool.make_bwf(frames=20, time_ref=1,
                               extra_chunks=[(b"LIST", b"odd"),  # 3-byte payload
                                             (b"cue ", b"\x00" * 24)])
        res = clip_bwf(wav, 4, 8)
        chunks = wavtool.parse_bwf(res.data)
        assert chunks[b"LIST"] == b"odd"
        assert chunks[b"cue "] == b"\x00" * 24
        assert len(chunks[b"data"]) == 8 * 4

    def test_output_can_be_clipped_again(self):
        wav = wavtool.make_bwf(frames=500, time_ref=40)
        first = clip_bwf(wav, 0, 400)
        second = clip_bwf(first.data, 10, 100)
        assert second.time_reference == 40 + 10
        assert second.frame_count == 100
        wavtool.parse_bwf(second.data)


class TestFormatRejection:
    def test_rifx_big_endian_rejected(self):
        wav = b"RIFX" + wavtool.make_bwf()[4:]
        expect_error("unsupported_container", wav)

    def test_rf64_rejected(self):
        wav = b"RF64" + wavtool.make_bwf()[4:]
        expect_error("unsupported_container", wav)

    def test_truncated_header(self):
        expect_error("truncated_header", b"RIFF\x00\x00")

    def test_not_wave(self):
        wav = wavtool.riff_wrap(b"AVI " + b"\x00" * 8)
        expect_error("not_wave", wav)

    def test_truncated_riff(self):
        wav = wavtool.make_bwf()
        wav = wav[:4] + struct.pack("<I", len(wav)) + wav[8:]  # declare too much
        expect_error("truncated_riff", wav)

    def test_truncated_chunk(self):
        body = b"WAVE" + b"fmt " + struct.pack("<I", 100) + b"\x00" * 16
        expect_error("truncated_chunk", wavtool.riff_wrap(body))

    def test_missing_fmt(self):
        body = b"WAVE" + wavtool.chunk(b"bext", wavtool.make_bext()) \
             + wavtool.chunk(b"data", b"\x00" * 8)
        expect_error("missing_chunk", wavtool.riff_wrap(body))

    def test_missing_bext(self):
        body = b"WAVE" + wavtool.chunk(b"fmt ", wavtool.make_fmt()) \
             + wavtool.chunk(b"data", b"\x00" * 8)
        expect_error("missing_chunk", wavtool.riff_wrap(body))

    def test_missing_data(self):
        body = b"WAVE" + wavtool.chunk(b"fmt ", wavtool.make_fmt()) \
             + wavtool.chunk(b"bext", wavtool.make_bext())
        expect_error("missing_chunk", wavtool.riff_wrap(body))

    def test_duplicate_fmt(self):
        fmt = wavtool.chunk(b"fmt ", wavtool.make_fmt())
        body = b"WAVE" + fmt + fmt \
             + wavtool.chunk(b"bext", wavtool.make_bext()) \
             + wavtool.chunk(b"data", b"\x00" * 8)
        expect_error("duplicate_chunk", wavtool.riff_wrap(body))

    def test_duplicate_data(self):
        data = wavtool.chunk(b"data", b"\x00" * 8)
        body = b"WAVE" + wavtool.chunk(b"fmt ", wavtool.make_fmt()) \
             + wavtool.chunk(b"bext", wavtool.make_bext()) + data + data
        expect_error("duplicate_chunk", wavtool.riff_wrap(body))

    def test_float_format_rejected(self):
        wav = wavtool.make_bwf(fmt_payload=wavtool.make_fmt(tag=3))
        expect_error("unsupported_encoding", wav)

    @pytest.mark.parametrize("bits", [8, 32])
    def test_bit_depth_rejected(self, bits):
        wav = wavtool.make_bwf(fmt_payload=wavtool.make_fmt(bits=bits))
        expect_error("unsupported_bit_depth", wav)

    @pytest.mark.parametrize("channels", [0, 9])
    def test_channel_count_rejected(self, channels):
        wav = wavtool.make_bwf(fmt_payload=wavtool.make_fmt(channels=channels))
        expect_error("unsupported_channels", wav)

    def test_bad_block_align(self):
        wav = wavtool.make_bwf(fmt_payload=wavtool.make_fmt(block_align=99))
        expect_error("bad_block_align", wav)

    def test_bad_byte_rate(self):
        wav = wavtool.make_bwf(fmt_payload=wavtool.make_fmt(byte_rate=1))
        expect_error("bad_byte_rate", wav)

    def test_truncated_fmt(self):
        wav = wavtool.make_bwf(fmt_payload=b"\x01\x00\x02")
        expect_error("truncated_fmt", wav)

    def test_truncated_bext(self):
        wav = wavtool.make_bwf(bext_payload=b"\x00" * 345)
        expect_error("truncated_bext", wav)

    def test_non_integral_frames(self):
        wav = wavtool.make_bwf(data_payload=b"\x00" * 10)  # 10 % 4 != 0
        err = expect_error("non_integral_frames", wav)
        assert err.status == 400


class TestRangeAndArithmetic:
    def test_start_past_end(self):
        wav = wavtool.make_bwf(frames=10)
        err = expect_error("range_out_of_bounds", wav, start=10, count=1)
        assert err.status == 422

    def test_count_overruns_end(self):
        wav = wavtool.make_bwf(frames=10)
        expect_error("range_out_of_bounds", wav, start=8, count=3)

    def test_huge_frame_numbers(self):
        wav = wavtool.make_bwf(frames=10)
        expect_error("range_out_of_bounds", wav, start=10 ** 30, count=10 ** 30)

    def test_negative_start(self):
        wav = wavtool.make_bwf(frames=10)
        expect_error("bad_start_frame", wav, start=-1, count=1)

    def test_zero_count(self):
        wav = wavtool.make_bwf(frames=10)
        expect_error("bad_frame_count", wav, start=0, count=0)

    def test_time_reference_overflow(self):
        wav = wavtool.make_bwf(frames=10, time_ref=(1 << 64) - 1)
        err = expect_error("time_reference_overflow", wav, start=1, count=1)
        assert err.status == 422

    def test_time_reference_at_boundary_ok(self):
        wav = wavtool.make_bwf(frames=10, time_ref=(1 << 64) - 2)
        res = clip_bwf(wav, 1, 1)
        assert res.time_reference == (1 << 64) - 1


class TestErrorLocation:
    def test_error_carries_chunk_and_offset(self):
        wav = wavtool.make_bwf(data_payload=b"\x00" * 10)
        with pytest.raises(BWFError) as excinfo:
            clip_bwf(wav, 0, 1)
        payload = excinfo.value.payload()["error"]
        assert payload["chunk"] == "data"
        assert isinstance(payload["offset"], int)
        assert payload["code"] == "non_integral_frames"
