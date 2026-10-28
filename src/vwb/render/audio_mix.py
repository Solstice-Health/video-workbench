"""Audio for an output: sample-exact trims from the time map, seam fades, two-pass EBU R128.

Audio keeps the source's words; only gain and fades change, and both are declared.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from vwb.canvas.model import Canvas
from vwb.media.probe import probe
from vwb.util.procs import ToolError, run, which
from vwb.workspace import Workspace


def _graph(canvas: Canvas, media_index: dict[str, int], media_info: dict) -> tuple[str, str]:
    fps = canvas.fps
    a = canvas.audio
    parts, labels = [], []
    n = len(canvas.time_map)
    for i, span in enumerate(canvas.time_map):
        info = media_info[span.media]
        source_fps = float(info.fps)
        t0, t1 = span.src_in / source_fps, span.src_out / source_fps
        dur = (span.src_out - span.src_in) / fps
        fin = (a.fade_in_ms if i == 0 else a.seam_fade_ms) / 1000.0
        fout = (a.fade_out_ms if i == n - 1 else a.seam_fade_ms) / 1000.0
        if info.has_audio:
            idx = media_index[span.media]
            head = f"[{idx}:a]atrim=start={t0:.9f}:end={t1:.9f},asetpts=PTS-STARTPTS,"
            # The canvas plays one source frame per output frame, so its audio
            # follows that same clock when native and output frame rates differ.
            speed = fps / source_fps
            while speed < 0.5 or speed > 2:
                factor = 0.5 if speed < 0.5 else 2
                head += f"atempo={factor},"
                speed /= factor
            head += f"atempo={speed:.9f},"
        else:
            head = "anullsrc=r=48000:cl=stereo,"
        parts.append(head + f"aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=end={dur:.9f},"
                     f"afade=t=in:st=0:d={fin:.3f},afade=t=out:st={max(0.0, dur - fout):.6f}:d={fout:.3f}[a{i}]")
        labels.append(f"[a{i}]")
    total = canvas.length / fps
    joined = "".join(labels) + (f"concat=n={n}:v=0:a=1[cat]" if n > 1 else "anull[cat]")
    graph = ";".join(parts + [joined])
    return graph, f"[cat]apad,atrim=end={total:.6f}"


def mix(ws: Workspace, canvas: Canvas, out_path: Path) -> dict:
    media_paths = {s.ref: s.path for s in ws.load().sources}
    used = sorted({s.media for s in canvas.time_map})
    infos = {m: probe(media_paths[m], with_keyframes=False) for m in used}
    if not canvas.time_map or not any(info.has_audio for info in infos.values()):
        duration = canvas.length / canvas.fps
        out_path.parent.mkdir(parents=True, exist_ok=True)
        run([which("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i",
             "anullsrc=r=48000:cl=stereo", "-t", f"{duration:.9f}", "-c:a", "aac", "-b:a", "256k",
             str(out_path)], expect_files=[out_path])
        return {"mode": "silent", "seconds": duration, "note": "Timeline has no source audio; silence is intentional."}
    media_index = {m: i for i, m in enumerate(used)}
    inputs = []
    for m in used:
        inputs += ["-i", media_paths[m]]
    graph, tail = _graph(canvas, media_index, infos)
    a = canvas.audio
    # pass 1: measure
    measure = run([which("ffmpeg"), "-hide_banner", "-nostdin", *inputs, "-filter_complex",
                   f"{graph};{tail},loudnorm=I={a.loudness_lufs}:TP={a.true_peak_dbtp}:LRA=11:print_format=json[out]",
                   "-map", "[out]", "-f", "null", "-"])
    text = measure.stderr.decode(errors="replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S)
    if not m:
        raise ToolError("loudnorm measurement failed")
    meas = json.loads(m.group(0))
    # pass 2: apply with measured values (linear when possible, so dynamics are kept)
    ln = (f"loudnorm=I={a.loudness_lufs}:TP={a.true_peak_dbtp}:LRA=11:measured_I={meas['input_i']}:"
          f"measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
          f"offset={meas['target_offset']}:linear=true")
    if not all(math.isfinite(float(meas[k])) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        ln = "anull"  # A silent selected interval has no finite loudness to normalize.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run([which("ffmpeg"), "-hide_banner", "-nostdin", "-y", *inputs, "-filter_complex",
         f"{graph};{tail},{ln},aresample=48000[out]", "-map", "[out]", "-c:a", "aac", "-b:a", "256k",
         "-ar", "48000", "-ac", "2", str(out_path)], expect_files=[out_path])
    return {"measured": meas, "target_lufs": a.loudness_lufs, "true_peak_dbtp": a.true_peak_dbtp}


def mux(video: Path, audio: Path, out_path: Path) -> Path:
    run([which("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-i", str(video), "-i", str(audio),
         "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", "-movflags", "+faststart", str(out_path)],
        expect_files=[out_path])
    return out_path
