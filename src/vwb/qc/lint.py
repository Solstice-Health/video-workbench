"""T0 lint: delivery and degenerate-edit gates on the delivered file.

Timeline-Bench's degenerate-edit checks (silent share, frozen share, internal silence,
channel balance) plus delivery, black spans, bars, loudness and flash frames.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from vwb.media import audio as audio_mod
from vwb.media.decode import read_frames
from vwb.qc.findings import Finding
from vwb.util.procs import ffprobe_json, run, which


def _filter_log(path: Path, vf: str | None = None, af: str | None = None) -> str:
    cmd = [which("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path)]
    if vf:
        cmd += ["-vf", vf]
    if af:
        cmd += ["-af", af]
    cmd += ["-f", "null", "-"]
    return run(cmd).stderr.decode(errors="replace")


def lint(path: Path, expected_frames: int, profile: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    data = ffprobe_json(path, "-show_streams", "-show_format", "-count_frames")
    v = next((s for s in data.get("streams", []) if s["codec_type"] == "video"), None)
    a = next((s for s in data.get("streams", []) if s["codec_type"] == "audio"), None)
    fps = profile["fps"]
    dur = float(data.get("format", {}).get("duration") or 0)
    problems = []
    if v is None:
        problems.append("no video stream")
    else:
        if (int(v["width"]), int(v["height"])) != (profile["width"], profile["height"]):
            problems.append(f"size {v['width']}x{v['height']} != {profile['width']}x{profile['height']}")
        if v.get("codec_name") != profile["video"]["codec"] or v.get("pix_fmt") != profile["video"]["pix_fmt"]:
            problems.append(f"codec {v.get('codec_name')}/{v.get('pix_fmt')}")
        if v.get("r_frame_rate") != f"{fps}/1":
            problems.append(f"frame rate {v.get('r_frame_rate')}")
        n = int(v.get("nb_read_frames") or v.get("nb_frames") or 0)
        if abs(n - expected_frames) > 0:
            problems.append(f"{n} frames, canvas declares {expected_frames}")
        if v.get("color_space") != "bt709":
            problems.append(f"colour space tag {v.get('color_space')}")
    if a is None:
        problems.append("no audio stream")
    elif int(a.get("sample_rate", 0)) != profile["audio"]["sample_rate"] or int(a.get("channels", 0)) != profile["audio"]["channels"]:
        problems.append(f"audio {a.get('sample_rate')} Hz x{a.get('channels')}")
    if dur > profile["max_duration_s"] + 0.05:
        problems.append(f"{dur:.2f}s over the {profile['max_duration_s']}s limit")
    out.append(Finding("t0.delivery", "fail" if problems else "pass", "CUE-MISMATCH" if problems else None,
                       "; ".join(problems) or f"{v['width']}x{v['height']} {v['codec_name']} {fps} fps, {dur:.2f}s, audio ok",
                       {"duration": dur, "problems": problems}))

    log = _filter_log(path, vf="blackdetect=d=0.3:pix_th=0.08,freezedetect=n=0.002:d=2")
    blacks = [(float(m.group(1)), float(m.group(2))) for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    long_black = [(s, e) for s, e in blacks if e - s > 0.5]
    out.append(Finding("t0.black", "fail" if long_black else "pass", "MOTION-FROZEN" if long_black else None,
                       f"{len(long_black)} black span(s) over 0.5 s" if long_black else "no black spans",
                       {"spans": long_black}))
    freezes = re.findall(r"freeze_duration: ([\d.]+)", log)
    frozen = sum(float(x) for x in freezes)
    share = frozen / dur if dur else 0
    out.append(Finding("t0.frozen_share", "fail" if share > 0.6 else "pass", "MOTION-FROZEN" if share > 0.6 else None,
                       f"frozen share {share:.0%}", {"frozen_s": round(frozen, 2), "share": round(share, 3)}))

    sil = audio_mod.silences(path, noise_db=-50, min_dur=0.8)
    internal = [(s, e) for s, e in sil if s > 0.3 and e < dur - 0.3 and e - s > 5.0]
    silent = sum(e - s for s, e in sil)
    out.append(Finding("t0.silence", "fail" if internal or (dur and silent / dur > 0.5) else "pass",
                       "AUDIO-CHANGED" if internal else None,
                       f"silent share {silent / dur:.0%}, {len(internal)} internal gap(s) over 5 s" if dur else "no audio",
                       {"silences": sil}))

    loud = audio_mod.loudness(path)
    target = profile["loudness"]
    ok_i = loud["integrated_lufs"] is not None and abs(loud["integrated_lufs"] - target["integrated_lufs"]) <= target["tolerance_lu"]
    ok_tp = loud["true_peak_dbtp"] is not None and loud["true_peak_dbtp"] <= target["true_peak_dbtp"] + 0.3
    out.append(Finding("t0.loudness", "pass" if ok_i and ok_tp else "fail", None if ok_i and ok_tp else "TOLERANCE",
                       f"I {loud['integrated_lufs']} LUFS (target {target['integrated_lufs']}), TP {loud['true_peak_dbtp']} dBTP",
                       loud, level=None if ok_i and ok_tp else "WARN"))
    bal = audio_mod.channel_balance_db(path)
    out.append(Finding("t0.channel_balance", "pass" if bal is not None and bal <= 6 else "fail",
                       None if bal is not None and bal <= 6 else "AUDIO-CHANGED", f"L/R difference {bal:.2f} dB" if bal is not None else "n/a",
                       {"db": bal}))

    # bars: rows or columns at the frame edge that are pure black in sampled frames
    n = expected_frames
    sample = [int(n * k / 6) for k in range(1, 6)]
    frames = read_frames(path, sample)
    bars = []
    for f, rgb in frames.items():
        luma = rgb.mean(axis=2)
        for name, strip in (("top", luma[:6]), ("bottom", luma[-6:]), ("left", luma[:, :6]), ("right", luma[:, -6:])):
            if float(strip.mean()) < 14 and float(strip.std()) < 3:
                bars.append({"frame": f, "edge": name})
    out.append(Finding("t0.bars", "fail" if bars else "pass", "TOLERANCE" if bars else None,
                       f"{len(bars)} black edge strip(s)" if bars else "no bars", {"bars": bars}, level="WARN" if bars else None))

    cuts_log = _filter_log(path, vf="scdet=threshold=12")
    cut_times = sorted(float(x) for x in re.findall(r"lavfi.scd.time: ([\d.]+)", cuts_log))
    flashes = [(a_, b_) for a_, b_ in zip(cut_times, cut_times[1:]) if (b_ - a_) * fps < 3]
    out.append(Finding("t0.flash_frames", "fail" if flashes else "pass", "SEAM-ROUGH" if flashes else None,
                       f"{len(flashes)} shot(s) under 3 frames" if flashes else f"{len(cut_times)} cut(s), none under 3 frames",
                       {"cuts": cut_times, "flashes": flashes}))
    return out
