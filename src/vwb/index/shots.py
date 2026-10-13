"""Shot boundaries with PySceneDetect's adaptive detector (frame-exact, half-open)."""

from __future__ import annotations

from pathlib import Path

from scenedetect import AdaptiveDetector, SceneManager, open_video


def detect_shots(path: Path | str, n_frames: int, fps: float, min_shot_s: float = 0.4,
                 threshold: float = 3.0) -> list[dict]:
    video = open_video(str(path), backend="opencv")
    manager = SceneManager()
    manager.auto_downscale = True
    manager.add_detector(AdaptiveDetector(adaptive_threshold=threshold,
                                          min_scene_len=max(2, int(round(min_shot_s * fps)))))
    manager.detect_scenes(video, show_progress=False)
    scenes = manager.get_scene_list(start_in_scene=True)
    cuts = sorted({int(start.frame_num) for start, _ in scenes} | {0})
    cuts = [c for c in cuts if 0 <= c < n_frames]
    bounds = cuts + [n_frames]
    shots = []
    for i, (f0, f1) in enumerate(zip(bounds[:-1], bounds[1:])):
        if f1 <= f0:
            continue
        shots.append({"id": i, "f0": f0, "f1": f1, "t0": f0 / fps, "t1": f1 / fps})
    for i, s in enumerate(shots):
        s["id"] = i
    return shots
