"""Source profile: what the file is, read from the container (spec: source_profile)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

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


def _fraction(text: str | None) -> Fraction:
    if not text or text in ("0/0", "0"):
        return Fraction(0)
    num, _, den = text.partition("/")
    return Fraction(int(num), int(den or 1))


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
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"No such media file: {path}")
    data = ffprobe_json(path, "-show_format", "-show_streams")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ToolError(f"No video stream in {path}")
    r_rate = _fraction(video.get("r_frame_rate"))
    avg_rate = _fraction(video.get("avg_frame_rate"))
    fps = r_rate or avg_rate
    duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0.0)
    n_frames = int(video.get("nb_frames") or round(duration * float(fps)))
    info = MediaInfo(
        path=str(path.resolve()),
        sha256=sha256_file(path),
        width=int(video["width"]),
        height=int(video["height"]),
        fps_num=fps.numerator,
        fps_den=fps.denominator,
        n_frames=n_frames,
        duration=duration,
        vcodec=video.get("codec_name", "?"),
        pix_fmt=video.get("pix_fmt", "?"),
        color_space=video.get("color_space"),
        color_transfer=video.get("color_transfer"),
        color_primaries=video.get("color_primaries"),
        color_range=video.get("color_range"),
        sar=video.get("sample_aspect_ratio", "1:1"),
        bit_rate=int(data.get("format", {}).get("bit_rate") or 0) or None,
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        audio_duration=float(audio["duration"]) if audio and audio.get("duration") else None,
        vfr=bool(avg_rate and r_rate and abs(float(avg_rate) - float(r_rate)) > 0.01),
    )
    if with_keyframes:
        info.keyframes = keyframe_indices(path, fps)
    return info
