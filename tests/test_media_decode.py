"""Offline decoder regressions: actual containers and controlled metadata/pipe failures."""
from __future__ import annotations

import importlib
import io
import os
import shutil
from fractions import Fraction

import numpy as np
import pytest

from vwb.media import decode
from vwb.media.decode import FrameReader, read_frames, sample_frames
from vwb.media.probe import probe, video_stream_info
from vwb.util.procs import ToolError, ffprobe_json, run, which

probe_module = importlib.import_module("vwb.media.probe")


def encode(path, *, frames=60, width=64, height=48, fps="30", codec="ffv1"):
    # Every frame differs in both color and geometry, so shifted/duplicated
    # frames cannot pass an identity comparison with the sequential decode.
    y, x = np.mgrid[:height, :width]
    pixels = np.stack([np.stack([(x * 3 + n * 4) % 256, (y * 5 + n * 7) % 256,
                                 (x + y + n * 11) % 256], axis=-1)
                       for n in range(frames)]).astype(np.uint8)
    extra = ["-pix_fmt", "yuv444p"] if codec == "ffv1" else ["-pix_fmt", "rgb24"]
    if codec == "libx264":
        extra = ["-pix_fmt", "yuv420p", "-g", "60", "-bf", "3"]
    run([which("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-framerate", fps, "-i", "pipe:0", "-an",
         "-c:v", codec, *extra, str(path)], input=pixels.tobytes(), expect_files=[path])
    return pixels


def sequential(path, width=64, height=48):
    raw = run([which("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:v:0", "-an", "-sn",
               "-vf", "scale=iw:ih:flags=bicubic+accurate_rnd+full_chroma_int:"
               "in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    return np.frombuffer(raw.stdout, np.uint8).reshape(-1, height, width, 3)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media-decode")
    mkv, mov, h264 = root / "no-stream-duration.mkv", root / "rgb.mov", root / "long-gop.mov"
    encode(mkv)
    rgb = encode(mov, codec="qtrle")
    encode(h264, codec="libx264", frames=75, fps="30000/1001")
    return mkv, mov, h264, rgb


def test_ffv1_mkv_container_duration_matches_probe_and_decodes_midpoint(media):
    mkv = media[0]
    raw = ffprobe_json(mkv, "-show_streams", "-show_format")
    assert "duration" not in raw["streams"][0]
    assert "nb_frames" not in raw["streams"][0]
    assert float(raw["format"]["duration"]) == 2
    info, reader = probe(mkv, with_keyframes=False), FrameReader(mkv)
    assert info.n_frames == reader.n_frames == 60
    assert info.fps == reader.fps == 30
    baseline = sequential(mkv)
    actual = read_frames(mkv, [59, 0, 30, 30])
    assert set(actual) == {0, 30, 59}
    for index in actual:
        np.testing.assert_array_equal(actual[index], baseline[index])


def test_mov_lossless_rgb_and_frame_boundaries(media):
    mov, expected = media[1], media[3]
    info, reader = probe(mov, with_keyframes=False), FrameReader(mov)
    assert info.n_frames == reader.n_frames == 60
    for index in (0, 1, 30, 59):
        np.testing.assert_array_equal(reader.frame(index), expected[index])
    assert list(reader.frames(60, 60)) == []
    assert read_frames(mov, []) == {}
    with pytest.raises(IndexError, match="60.*outside.*60"):
        read_frames(mov, [0, 60])
    with pytest.raises(IndexError, match="outside"):
        reader.frame(60)


def test_fractional_fps_long_gop_seek_and_stepped_decode_match_sequential(media):
    path = media[2]
    reader, expected = FrameReader(path), sequential(path)
    assert reader.fps == Fraction(30000, 1001)
    assert reader.n_frames == len(expected) == 75
    for index, actual in read_frames(path, [0, 1, 29, 58, 59, 60, 61, 74]).items():
        np.testing.assert_array_equal(actual, expected[index])
    samples = dict(sample_frames(path, 7, start=13, end=75))
    assert list(samples) == list(range(13, 75, 7))
    for index, actual in samples.items():
        np.testing.assert_array_equal(actual, expected[index])


def test_first_video_stream_is_explicitly_decoded(tmp_path, media):
    # The second stream is larger and marked default; automatic ffmpeg stream
    # selection would disagree with the metadata's first-video-stream choice.
    larger = tmp_path / "larger.mov"
    encode(larger, frames=2, width=96, height=80, codec="qtrle")
    output = tmp_path / "two-videos.mov"
    run([which("ffmpeg"), "-v", "error", "-i", str(media[1]), "-i", str(larger),
         "-map", "0:v:0", "-map", "1:v:0", "-c", "copy", "-disposition:v:0", "0",
         "-disposition:v:1", "default", str(output)], expect_files=[output])
    reader = FrameReader(output)
    assert (reader.src_w, reader.src_h) == (64, 48)
    np.testing.assert_array_equal(reader.frame(1), media[3][1])


def test_bt709_limited_range_color_conversion_is_preserved(tmp_path):
    path = tmp_path / "bt709.mkv"
    # A known constant YUV color distinguishes BT.709 from BT.601 conversion.
    planes = np.array([81] * 256 + [90] * 256 + [240] * 256, dtype=np.uint8)
    run([which("ffmpeg"), "-v", "error", "-f", "rawvideo", "-pix_fmt", "yuv444p", "-s", "16x16",
         "-framerate", "30", "-i", "pipe:0", "-c:v", "ffv1", str(path)],
        input=planes.tobytes(), expect_files=[path])
    actual = FrameReader(path).frame(0)
    # BT.709 limited-range YUV -> RGB, rounded by swscale.
    y, u, v = (81 - 16) * 255 / 219, (90 - 128) * 255 / 224, (240 - 128) * 255 / 224
    expected = np.clip([y + 1.5748 * v, y - .1873 * u - .4681 * v, y + 1.8556 * u], 0, 255)
    np.testing.assert_allclose(actual[0, 0], expected, atol=1)


@pytest.fixture
def metadata(tmp_path, monkeypatch):
    path = tmp_path / "metadata.bin"
    path.write_bytes(b"synthetic metadata input")
    data = {"streams": [{"codec_type": "video", "width": 2, "height": 2,
                         "r_frame_rate": "30/1", "avg_frame_rate": "30/1"}], "format": {}}
    calls = []

    def fake_probe(*args):
        calls.append(args)
        return data

    monkeypatch.setattr(probe_module, "ffprobe_json", fake_probe)
    return path, data, calls


@pytest.mark.parametrize(("video", "fmt", "n", "duration"), [
    ({}, {"duration": "2"}, 60, 2),
    ({"duration": "N/A", "nb_frames": "N/A"}, {"duration": "2"}, 60, 2),
    ({"duration": "0", "nb_frames": "0"}, {"duration": "2"}, 60, 2),
    ({"duration": "1", "nb_frames": "30"}, {"duration": "5"}, 30, 1),
    ({"nb_frames": "60"}, {}, 60, 2),
    ({"nb_frames": "60", "duration": "N/A"}, {"duration": "N/A"}, 60, 2),
    ({"duration_ts": "2000", "time_base": "1/1000"}, {"duration": "5"}, 60, 2),
    ({"nb_frames": "N/A", "duration": "0.1"}, {}, 3, .1),
    ({"nb_frames": "1", "duration": "0.033"}, {}, 1, .033),
])
def test_metadata_fallbacks_are_shared_by_probe_and_reader(metadata, video, fmt, n, duration):
    path, data, calls = metadata
    data["streams"][0].update(video)
    data["format"].update(fmt)
    info, reader = probe(path, with_keyframes=False), FrameReader(path)
    assert info.n_frames == reader.n_frames == n
    assert info.duration == duration
    assert info.fps == reader.fps == 30
    assert len(calls) == 1


@pytest.mark.parametrize("rate", [None, "0/0", "0", "N/A", "-30/1", "1/0", "nan", "inf", "bad"])
def test_invalid_nominal_rate_falls_back_to_average(metadata, rate):
    path, data, _ = metadata
    data["streams"][0].update(r_frame_rate=rate, avg_frame_rate="24000/1001", nb_frames="48")
    info = video_stream_info(path)
    assert info.fps == Fraction(24000, 1001)
    assert info.duration == pytest.approx(2.002)


@pytest.mark.parametrize(("fields", "message"), [
    ({}, "frame count"),
    ({"duration": "N/A", "nb_frames": "N/A"}, "frame count"),
    ({"duration": "0", "nb_frames": "0"}, "frame count"),
    ({"duration": "nan", "nb_frames": "-1"}, "frame count"),
    ({"duration": "inf"}, "frame count"),
    ({"duration": "1", "r_frame_rate": "0/0", "avg_frame_rate": "N/A"}, "frame rate"),
    ({"duration": "1", "width": 0}, "dimensions"),
    ({"duration": "1", "height": "N/A"}, "dimensions"),
])
def test_unknown_or_invalid_metadata_has_actionable_error(metadata, fields, message):
    path, data, _ = metadata
    data["streams"][0].update(fields)
    for factory in (FrameReader, lambda p: probe(p, with_keyframes=False)):
        with pytest.raises(ToolError, match=message):
            factory(path)


def test_optional_audio_and_bitrate_na_values_do_not_break_probe(metadata):
    path, data, _ = metadata
    data["streams"][0]["nb_frames"] = "60"
    data["streams"].append({"codec_type": "audio", "sample_rate": "N/A", "channels": "N/A", "duration": "N/A"})
    data["format"]["bit_rate"] = "N/A"
    result = probe(path, with_keyframes=False)
    assert result.has_audio
    assert result.bit_rate is result.audio_duration is result.channels is result.sample_rate is None


def test_missing_video_and_nonfiles_are_explicit(metadata):
    path, data, _ = metadata
    data["streams"] = [{"codec_type": "audio"}]
    with pytest.raises(ToolError, match="No video stream"):
        FrameReader(path)
    with pytest.raises(ToolError, match="Cannot stat media file"):
        FrameReader(path.with_name("absent"))
    with pytest.raises(ToolError, match="Not a regular"):
        FrameReader(path.parent)


@pytest.mark.parametrize("index", [-1, 1.5, "1", True, float("nan")])
def test_invalid_sparse_indices_never_truncate_or_clamp(media, index):
    with pytest.raises(ValueError, match="frame index must be an integer"):
        read_frames(media[0], [0, index])


@pytest.mark.parametrize(("start", "end", "step", "error"), [
    (-1, 2, 1, ValueError), (0, 61, 1, IndexError), (61, 61, 1, IndexError),
    (3, 2, 1, ValueError), (0, 2, 0, ValueError), (0, 2, -1, ValueError),
    (0, 2, 1.5, ValueError), (1.5, 2, 1, ValueError), (0, 2.5, 1, ValueError),
])
def test_invalid_ranges_are_rejected(media, start, end, step, error):
    with pytest.raises(error):
        list(FrameReader(media[0]).frames(start, end, step))


@pytest.mark.parametrize("size", [(0, 2), (2, -1), (2.5, 2), (True, 2), (), (2,)])
def test_invalid_output_dimensions_are_rejected(media, size):
    with pytest.raises(ValueError):
        FrameReader(media[0], size=size)


class FakeProcess:
    def __init__(self, payload, *, code=0, stderr=b"", short_reads=False):
        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(5, size))

        self.stdout = ShortReads(payload) if short_reads else io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr)
        self.code, self.returncode, self.killed = code, None, False

    def wait(self, **kwargs):
        self.returncode = self.code
        return self.code

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.code = -9


def fake_reader(metadata, monkeypatch, payload, **kwargs):
    path, data, _ = metadata
    data["streams"][0]["nb_frames"] = "3"
    proc = FakeProcess(payload, **kwargs)
    monkeypatch.setattr(decode.subprocess, "Popen", lambda *a, **kw: proc)
    return path, proc


@pytest.mark.parametrize("payload", [b"", bytes(12), bytes(12 + 5)])
def test_short_decode_never_returns_partial_dictionary(metadata, monkeypatch, payload):
    path, proc = fake_reader(metadata, monkeypatch, payload, stderr=b"broken packet")
    with pytest.raises(ToolError, match=r"Incomplete.*requested 3.*decoded.*missing frame.*broken packet"):
        read_frames(path, [0, 2])
    assert proc.stdout.closed and proc.stderr.closed


def test_short_pipe_reads_are_accumulated(metadata, monkeypatch):
    path, proc = fake_reader(metadata, monkeypatch, bytes(range(36)), short_reads=True)
    actual = read_frames(path, [0, 1, 2])
    assert set(actual) == {0, 1, 2}
    np.testing.assert_array_equal(actual[2].ravel(), np.arange(24, 36))
    assert not proc.killed and proc.stdout.closed and proc.stderr.closed


def test_nonzero_exit_after_full_output_is_not_success(metadata, monkeypatch):
    path, proc = fake_reader(metadata, monkeypatch, bytes(12), code=7, stderr=b"decoder failed")
    with pytest.raises(ToolError, match=r"exit 7.*decoder failed"):
        FrameReader(path).frame(0)
    assert not proc.killed and proc.stdout.closed and proc.stderr.closed


def test_generator_close_cleans_up_child_and_pipes(metadata, monkeypatch):
    path, proc = fake_reader(metadata, monkeypatch, bytes(36))
    frames = FrameReader(path).frames(0, 3)
    next(frames)
    frames.close()
    assert proc.killed and proc.stdout.closed and proc.stderr.closed


def test_bounded_stderr_tail_keeps_diagnostic(metadata, monkeypatch):
    path, _ = fake_reader(metadata, monkeypatch, b"", code=2, stderr=b"x" * 100_000 + b"final diagnostic")
    with pytest.raises(ToolError, match="final diagnostic") as error:
        FrameReader(path).frame(0)
    assert len(str(error.value)) < 2500


def test_decoder_checks_missing_requested_keys_even_if_iterator_is_broken(metadata, monkeypatch):
    path, data, _ = metadata
    data["streams"][0]["nb_frames"] = "3"
    monkeypatch.setattr(FrameReader, "frames", lambda *a: iter([(0, np.zeros((2, 2, 3), np.uint8))]))
    with pytest.raises(ToolError, match=r"Missing requested frames.*2"):
        read_frames(path, [0, 2])


def test_lightweight_metadata_and_decodes_never_hash_whole_file(media, monkeypatch):
    monkeypatch.setattr(probe_module, "sha256_file", lambda *a: pytest.fail("decoder must not hash media"))
    for path in media[:3]:
        assert video_stream_info(path).n_frames > 0
        assert FrameReader(path).frame(1).shape == (48, 64, 3)
        assert set(read_frames(path, [0, 2])) == {0, 2}


def test_replacing_work_file_invalidates_cache_and_existing_reader(tmp_path, media):
    path = tmp_path / "work-video.mkv"
    shutil.copyfile(media[0], path)
    old = FrameReader(path)
    assert old.n_frames == 60
    new_file = tmp_path / "replacement.mov"
    encode(new_file, frames=6, width=32, height=24, fps="24", codec="qtrle")
    new_file.replace(path)
    fresh = video_stream_info(path)
    info = probe(path, with_keyframes=False)
    assert fresh.n_frames == info.n_frames == 6
    assert fresh.width == info.width == 32
    assert fresh.fps == info.fps == 24
    assert read_frames(path, [5])[5].shape == (24, 32, 3)
    with pytest.raises(ToolError, match="changed while being read"):
        old.frame(0)


def test_same_size_in_place_edit_invalidates_cache_even_with_restored_mtime(metadata):
    path, data, calls = metadata
    data["streams"][0]["nb_frames"] = "3"
    assert video_stream_info(path).n_frames == 3
    assert video_stream_info(path).n_frames == 3
    assert len(calls) == 1
    stat = path.stat()
    path.write_bytes(b"x" * stat.st_size)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    data["streams"][0]["nb_frames"] = "4"
    assert video_stream_info(path).n_frames == 4
    assert len(calls) == 2


def test_change_during_probe_is_not_cached(metadata, monkeypatch):
    path, data, _ = metadata
    data["streams"][0]["nb_frames"] = "3"

    def changed(*args):
        path.write_bytes(path.read_bytes() + b"x")
        return data

    monkeypatch.setattr(probe_module, "ffprobe_json", changed)
    with pytest.raises(ToolError, match="changed while being read"):
        FrameReader(path)


def test_change_during_decode_is_reported(metadata, monkeypatch):
    path, _ = fake_reader(metadata, monkeypatch, bytes(36))
    frames = FrameReader(path).frames(0, 3)
    next(frames)
    path.write_bytes(b"replaced during decode")
    with pytest.raises(ToolError, match="changed while being read"):
        list(frames)


def test_probe_detects_change_during_hash(metadata, monkeypatch):
    path, data, _ = metadata
    data["streams"][0]["nb_frames"] = "3"

    def changed(*args):
        path.write_bytes(b"replaced during hash")
        return "synthetic-hash"

    monkeypatch.setattr(probe_module, "sha256_file", changed)
    with pytest.raises(ToolError, match="changed while being read"):
        probe(path, with_keyframes=False)


def test_truncated_real_ffv1_reports_incomplete_decode(tmp_path, media):
    path = tmp_path / "truncated.mkv"
    data = media[0].read_bytes()
    # Retain the container timing header, but remove the final frame packets.
    path.write_bytes(data[:len(data) * 3 // 4])
    assert video_stream_info(path).n_frames == 60
    with pytest.raises(ToolError, match=r"Incomplete.*missing frame"):
        read_frames(path, [0, 59])


def test_short_native_decode_with_overstated_metadata_reports_missing_frame(tmp_path, media, monkeypatch):
    # Inaccurate timing without a usable per-stream tag can still overestimate
    # a video's tail. The native decoder must report the gap, never return a subset.
    path = tmp_path / "overstated.mkv"
    shutil.copyfile(media[0], path)
    native_probe = probe_module.ffprobe_json

    def overstated(*args):
        data = native_probe(*args)
        data["streams"][0].get("tags", {}).pop("DURATION", None)
        data["format"]["duration"] = "3.000"
        return data

    monkeypatch.setattr(probe_module, "ffprobe_json", overstated)
    assert FrameReader(path).n_frames == 90
    with pytest.raises(ToolError, match=r"Incomplete.*missing frame"):
        read_frames(path, [59, 60])


def test_longer_audio_does_not_extend_mkv_video_count_or_sampling(tmp_path):
    path = tmp_path / "two-second-video-five-second-audio.mkv"
    run([which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x48:r=30:d=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=5",
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(path)],
        timeout=10, expect_files=[path])
    raw = ffprobe_json(path, "-show_streams", "-show_format")
    assert float(raw["format"]["duration"]) == 5
    video = raw["streams"][0]
    assert "duration" not in video and "nb_frames" not in video
    assert video["tags"]["DURATION"] == "00:00:02.000000000"
    info, reader = probe(path, with_keyframes=False), FrameReader(path)
    assert info.duration == 2
    assert info.n_frames == reader.n_frames == 60
    assert list(dict(sample_frames(path, 6))) == list(range(0, 60, 6))
    assert set(read_frames(path, [info.n_frames // 2, 59])) == {30, 59}


@pytest.mark.parametrize(("tags", "start", "n", "duration"), [
    ({"DURATION": "00:00:02.000000000"}, "0.000000", 60, 2),
    ({"duration": "00:00:02.000000000"}, None, 60, 2),
    ({"DURATION": "00:00:03.000000000"}, "1.000000", 60, 2),
    ({"DURATION": "01:02:03.500000000"}, "0", 111705, 3723.5),
    ({"DURATION": "00:00:00.033333333"}, "0", 1, .033333333),
])
def test_stream_duration_tags_override_container_duration(metadata, tags, start, n, duration):
    path, data, _ = metadata
    data["streams"][0].update(tags=tags, start_time=start)
    data["format"]["duration"] = "5000"
    info, reader = probe(path, with_keyframes=False), FrameReader(path)
    assert info.n_frames == reader.n_frames == n
    assert info.duration == duration


@pytest.mark.parametrize("value", [None, "N/A", "", "00:60:00", "00:00:60", "-01:00:00", "nan", "00:00:00"])
def test_unusable_duration_tag_uses_other_metadata_or_explicit_error(metadata, value):
    path, data, _ = metadata
    data["streams"][0]["tags"] = {"DURATION": value}
    with pytest.raises(ToolError, match="frame count"):
        FrameReader(path)
    data["format"]["duration"] = "2"
    assert FrameReader(path).n_frames == 60


@pytest.mark.parametrize(("offset", "audio"), [(1, True), (2, False), (-1, True)])
def test_offset_video_returns_correct_frame_ordinals(tmp_path, offset, audio):
    path = tmp_path / "offset.mkv"
    # Unique lossless RGB pixels identify every frame without timestamp-based
    # expectations. Include later-video, whole-container offset and negative PTS.
    expected = np.stack([np.full((16, 16, 3), (n * 4, n * 3, n * 2), np.uint8) for n in range(60)])
    cmd = [which("ffmpeg"), "-v", "error", "-itsoffset", str(offset), "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-s", "16x16", "-framerate", "30", "-i", "pipe:0"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=5",
                "-map", "0:v:0", "-map", "1:a:0", "-c:a", "pcm_s16le"]
    cmd += ["-c:v", "ffv1", "-pix_fmt", "bgr0", "-fps_mode", "passthrough",
            "-avoid_negative_ts", "disabled", str(path)]
    run(cmd, input=expected.tobytes(), timeout=10, expect_files=[path])
    reader = FrameReader(path)
    assert reader.n_frames == 60
    assert (reader.start_time > 0) if offset > 0 else (reader.start_time < 0)
    for index in (0, 1, 15, 45, 59):
        np.testing.assert_array_equal(reader.frame(index), expected[index])
    for index, pixels in read_frames(path, [1, 15, 45, 59]).items():
        np.testing.assert_array_equal(pixels, expected[index])
    samples = dict(sample_frames(path, 7, start=13))
    assert list(samples) == list(range(13, 60, 7))
    for index, pixels in samples.items():
        np.testing.assert_array_equal(pixels, expected[index])


@pytest.mark.parametrize(("video", "fmt", "start"), [
    ({"start_time": "1.25"}, {"start_time": "0"}, Fraction(5, 4)),
    ({"start_time": "0"}, {"start_time": "2"}, Fraction(0)),
    ({"start_time": "N/A", "start_pts": "1500", "time_base": "1/1000"}, {}, Fraction(3, 2)),
    ({"start_time": "N/A"}, {"start_time": "2"}, Fraction(2)),
    ({"start_time": "-1"}, {}, Fraction(-1)),
])
def test_video_start_time_metadata_fallbacks(metadata, video, fmt, start):
    path, data, _ = metadata
    data["streams"][0].update(nb_frames="60", **video)
    data["format"].update(fmt)
    assert video_stream_info(path).start_time == FrameReader(path).start_time == start
