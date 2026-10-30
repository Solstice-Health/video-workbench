"""Source profile: what the file is, read from the container (spec: source_profile)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from stat import S_ISREG

from vwb.util.hashing import sha256_file
from vwb.util.procs import ToolError, ffprobe_json, run, which


@dataclass
class MediaInfo:
    path: str
    sha256: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    n_frames: int
    duration: float
    vcodec: str
    pix_fmt: str
    color_space: str | None
    color_transfer: str | None
    color_primaries: str | None
    color_range: str | None
    sar: str
    bit_rate: int | None
    has_audio: bool
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    audio_duration: float | None = None
    keyframes: list[int] = field(default_factory=list)
    vfr: bool = False

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_num, self.fps_den)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fps"] = float(self.fps)
        return d


@dataclass(frozen=True)
class VideoStreamInfo:
    """Shared, hash-free video timing/geometry used by probing and decoding.

    Counts inferred from duration are estimates (not a decode or frame scan).
    Decoders must still verify that every requested frame actually arrives.
    """

    width: int
    height: int
    fps: Fraction
    n_frames: int
    duration: float
    vfr: bool
    start_time: Fraction = Fraction(0)


@dataclass(frozen=True)
class _Metadata:
    path: str
    identity: tuple[int, ...]
    video: dict
    audio: dict | None
    format: dict
    info: VideoStreamInfo


def _fraction(text: str | None) -> Fraction:
    try:
        value = Fraction(str(text))
        return value if value > 0 else Fraction(0)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _positive_int(value) -> int | None:
    number = _fraction(value)
    return int(number) if number > 0 and number.denominator == 1 else None


def _timestamp(value) -> Fraction | None:
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def _video_start(video: dict, fmt: dict) -> Fraction:
    start = _timestamp(video.get("start_time"))
    if start is None:
        pts, time_base = _timestamp(video.get("start_pts")), _fraction(video.get("time_base"))
        if pts is not None and time_base:
            start = pts * time_base
    if start is None:
        start = _timestamp(fmt.get("start_time"))
    return start if start is not None else Fraction(0)


def _tag_duration(video: dict) -> Fraction:
    """Read Matroska's per-stream HH:MM:SS.fraction duration tag.

    FFmpeg writes the stream's end timestamp here, so a delayed stream's start
    must be subtracted. Container duration can instead include a longer audio
    track and must not take precedence over this video-specific metadata.
    """
    tags = video.get("tags") or {}
    value = tags.get("DURATION", tags.get("duration"))
    if not isinstance(value, str):
        return Fraction(0)
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)", value.strip())
    if match is None:
        return Fraction(0)
    hours, minutes, seconds = match.groups()
    end = int(hours) * 3600 + int(minutes) * 60 + Fraction(seconds)
    return max(Fraction(0), end - _video_start(video, {}))


def _file_identity(path: Path | str) -> tuple[int, ...]:
    """Invalidate cached metadata on replacement and ordinary in-place writes."""
    try:
        st = Path(path).stat()
    except OSError as exc:
        raise ToolError(f"Cannot stat media file {path}: {exc}") from exc
    if not S_ISREG(st.st_mode):
        raise ToolError(f"Not a regular media file: {path}")
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _assert_identity(path: str, identity: tuple[int, ...]) -> None:
    if _file_identity(path) != identity:
        raise ToolError(f"Media file changed while being read: {path}; reopen it before decoding or probing")


def _video_info(video: dict, fmt: dict, path: str) -> VideoStreamInfo:
    width, height = _positive_int(video.get("width")), _positive_int(video.get("height"))
    if width is None or height is None:
        raise ToolError(f"Invalid video dimensions in {path}: {video.get('width')} x {video.get('height')}")
    r_rate = _fraction(video.get("r_frame_rate"))
    avg_rate = _fraction(video.get("avg_frame_rate"))
    fps = r_rate or avg_rate
    if not fps:
        raise ToolError(f"Cannot determine a positive video frame rate for {path}")
    # Matroska commonly has a per-stream DURATION tag instead of duration;
    # its format.duration may include a longer audio track. MOV has nb_frames.
    # N/A and zero are unavailable values, not a zero-frame video.
    duration = (_fraction(video.get("duration"))
                or _fraction(video.get("duration_ts")) * _fraction(video.get("time_base"))
                or _tag_duration(video)
                or _fraction(fmt.get("duration")))
    n_frames = _positive_int(video.get("nb_frames"))
    if n_frames is None and duration:
        n_frames = round(duration * fps)
    if not n_frames:
        raise ToolError(f"Cannot determine a positive video frame count for {path}: "
                        "no usable nb_frames or duration; remux with timing metadata or count frames explicitly")
    duration = duration or Fraction(n_frames, 1) / fps
    return VideoStreamInfo(width, height, fps, n_frames, float(duration),
                           bool(avg_rate and r_rate and abs(float(avg_rate - r_rate)) > 0.01),
                           start_time=_video_start(video, fmt))


@lru_cache(maxsize=64)
def _cached_metadata(path: str, identity: tuple[int, ...]) -> _Metadata:
    data = ffprobe_json(path, "-show_format", "-show_streams")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ToolError(f"No video stream in {path}")
    fmt = data.get("format", {})
    info = _video_info(video, fmt, path)
    _assert_identity(path, identity)
    return _Metadata(path, identity, video, audio, fmt, info)


def _metadata(path: Path | str) -> _Metadata:
    # Keep the pathname (including symlinks), so replacing its target invalidates
    # an already-open reader too. Absolute paths avoid cwd-dependent cache keys.
    path = str(Path(path).absolute())
    return _cached_metadata(path, _file_identity(path))


def video_stream_info(path: Path | str) -> VideoStreamInfo:
    """Read cached timing and geometry without hashing the media's contents."""
    return _metadata(path).info


def keyframe_indices(path: Path | str, fps: Fraction) -> list[int]:
    proc = run([which("ffprobe"), "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)])
    out = []
    for line in proc.stdout.decode().split():
        try:
            out.append(int(round(float(line.strip().strip(",")) * float(fps))))
        except ValueError:
            continue
    return out


def probe(path: Path | str, with_keyframes: bool = True) -> MediaInfo:
    metadata = _metadata(path)
    video, audio, timing = metadata.video, metadata.audio, metadata.info
    info = MediaInfo(
        path=str(Path(metadata.path).resolve()),
        sha256=sha256_file(metadata.path),
        width=timing.width,
        height=timing.height,
        fps_num=timing.fps.numerator,
        fps_den=timing.fps.denominator,
        n_frames=timing.n_frames,
        duration=timing.duration,
        vcodec=video.get("codec_name", "?"),
        pix_fmt=video.get("pix_fmt", "?"),
        color_space=video.get("color_space"),
        color_transfer=video.get("color_transfer"),
        color_primaries=video.get("color_primaries"),
        color_range=video.get("color_range"),
        sar=video.get("sample_aspect_ratio", "1:1"),
        bit_rate=_positive_int(metadata.format.get("bit_rate")),
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=_positive_int(audio.get("sample_rate")) if audio else None,
        channels=_positive_int(audio.get("channels")) if audio else None,
        audio_duration=float(_fraction(audio.get("duration"))) or None if audio else None,
        vfr=timing.vfr,
    )
    if with_keyframes:
        info.keyframes = keyframe_indices(metadata.path, timing.fps)
    _assert_identity(metadata.path, metadata.identity)
    return info
