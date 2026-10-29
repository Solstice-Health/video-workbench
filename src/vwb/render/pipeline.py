"""Render one canvas version: plates -> composite -> x264 -> audio -> mux -> receipt."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from vwb.canvas.compile import plan
from vwb.canvas.model import Canvas
from vwb.canvas.paths import checked_path, output_id, read_record, version_number
from vwb.canvas.renderers import validate_renderer
from vwb.canvas.store import CanvasStore
from vwb.config import load_profile
from vwb.render import audio_mix, plates
from vwb.render.compositor import COMPOSITOR_VERSION, Compositor, encode
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import write_json
from vwb.util.procs import ffmpeg_version
from vwb.workspace import Workspace
from vwb.workspace.ledger import Ledger
from vwb.workspace.registry import Registry


def render_dir(ws: Workspace, output: str, version: int) -> Path:
    return checked_path(ws.root, "renders", output_id(output), f"v{version_number(version)}")


def render_receipt(ws: Workspace, output: str, version: int, default: Any = None) -> Any:
    return read_record(ws.root, "renders", output_id(output), f"v{version_number(version)}", "receipt.json", default=default)


def plate_jobs(canvas: Canvas, ws: Workspace | None = None) -> list[tuple[str, dict[str, Any]]]:
    reg = Registry(ws.assets) if ws is not None else None
    return [("Scene", s.props) for s in plan(canvas, reg) if s.kind == "plate" and s.props is not None]


def ensure_plates(ws: Workspace, canvases: list[Canvas]) -> None:
    jobs = [j for c in canvases for j in plate_jobs(c, ws)]
    if jobs:
        plates.render(ws.plates, jobs)


def render(ws: Workspace, output: str, version: int | None = None, actor: str = "render", force: bool = False) -> dict[str, Any]:
    canvas = CanvasStore(ws.canvas).get(output, version)
    validate_renderer(canvas)
    out_dir = render_dir(ws, output, canvas.version)
    receipt_path = out_dir / "receipt.json"
    if receipt_path.exists() and not force:
        return render_receipt(ws, output, canvas.version)
    out_dir.mkdir(parents=True, exist_ok=True)
    from filelock import FileLock
    with FileLock(str(out_dir / ".render.lock")):          # one render of a version at a time; the next reuses it
        if receipt_path.exists() and not force:
            return render_receipt(ws, output, canvas.version)
        return _render_locked(ws, canvas, output, out_dir, receipt_path, actor)


def _render_locked(ws: Workspace, canvas: Canvas, output: str, out_dir: Path, receipt_path: Path, actor: str) -> dict[str, Any]:
    t0 = time.time()
    profile = load_profile(canvas.profile)
    ensure_plates(ws, [canvas])
    comp = Compositor(ws, canvas)
    video = encode(comp, out_dir / "video.mp4", profile)
    audio_info = audio_mix.mix(ws, canvas, out_dir / "audio.m4a")
    final = audio_mix.mux(video, out_dir / "audio.m4a", out_dir / f"{output}_v{canvas.version}.mp4")
    reg = Registry(ws.assets)
    req = ws.load()
    used_media = {sp.media for sp in canvas.time_map}
    inputs = {"canvas_sha256": canvas.provenance.get("sha256"),
              "media": {s.ref: s.sha256 for s in req.sources if s.ref in used_media},
              "plates": {s.plate_id: sha256_file(comp.plate_path(s)) for s in comp.steps if s.kind == "plate"},
              "assets": {l.asset: reg.get(l.asset)["sha256"] for s in comp.steps for l in s.layers
                         if s.kind in ("image", "clip")}}
    receipt = {"output": output, "version": canvas.version, "file": str(final), "sha256": sha256_file(final),
               "frames": canvas.length, "fps": canvas.fps, "seconds": round(canvas.length / canvas.fps, 3),
               "inputs": inputs,
               "tools": {"compositor": COMPOSITOR_VERSION, "ffmpeg": ffmpeg_version(),
                         "renderer": canvas.renderer, "generator": validate_renderer(canvas).pin,
                         "x264": {k: profile["video"][k] for k in ("crf", "preset", "profile")}},
               "audio": audio_info, "render_seconds": round(time.time() - t0, 1), "attestation": "rendered-from-canvas"}
    write_json(receipt_path, receipt)
    Ledger(ws.ledger / "ledger.jsonl").append("render.done", actor, {"file": ws.rel(final), "sha256": receipt["sha256"],
                                                                      "seconds": receipt["render_seconds"]},
                                              output=output, version=canvas.version)
    return receipt


def render_many(ws: Workspace, outputs: list[str], actor: str = "render", workers: int = 3,
                force: bool = False) -> list[dict[str, Any]]:
    """Every missing plate in one Remotion batch, then composite and encode in parallel."""
    store = CanvasStore(ws.canvas)
    canvases = [store.get(o) for o in outputs]
    ensure_plates(ws, canvases)

    def one(c: Canvas) -> dict[str, Any]:
        try:
            return render(ws, c.output, c.version, actor=actor, force=force)
        except Exception as exc:  # one failed output must not sink the batch
            return {"output": c.output, "version": c.version, "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, canvases))
