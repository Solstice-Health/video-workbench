"""Compile an agent-written canvas: load, check, expand derived parts, plan the render.

  load_working   work/outputs/<id>/canvas.json (the agent's source) -> Canvas
  check          errors block a commit; warnings are for the author
  expand         copy entries for text layers, caption pages from the transcript
  plan           z-ordered steps: video, image and clip layers composited in Python, runs of
                 text/caption/shape layers rendered as one alpha plate each (Remotion)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vwb.canvas import copydeck
from vwb.canvas.anim import all_values, is_keyed, value_at
from vwb.canvas.model import (
    Canvas,
    CaptionsLayer,
    ClipLayer,
    ImageLayer,
    ShapeLayer,
    TextLayer,
    VideoLayer,
    font_library,
)
from vwb.config import load_profile
from vwb.index.ocr import CLAIM_HINT
from vwb.index.store import IndexStore
from vwb.media.probe import probe
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry

PLATE_PIN = "remotion@4.0.534:Scene@4"
PLATE_TYPES = (TextLayer, CaptionsLayer, ShapeLayer)


def working_path(ws: Workspace, output: str) -> Path:
    return ws.work / "outputs" / output / "canvas.json"


def load_working(ws: Workspace, output: str) -> Canvas:
    data = read_json(working_path(ws, output))
    if data is None:
        raise FileNotFoundError(f"no working canvas for {output}; create it with canvas_new")
    req = ws.load()
    data.setdefault("request", req.id)
    data["output"] = output
    data.setdefault("profile", "youtube_shorts")
    if "format" not in data:
        prof = load_profile(data["profile"])
        data["format"] = {"width": prof["width"], "height": prof["height"], "fps": prof["fps"]}
    data.pop("copy", None)          # the deck is the only source of copy; commit fills it in
    data.pop("version", None)
    data.pop("parent", None)
    try:
        return Canvas.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_readable(exc)) from exc


def _readable(exc: ValidationError) -> str:
    lines = []
    for e in exc.errors()[:12]:
        loc = ".".join(str(x) for x in e["loc"])
        lines.append(f"{loc}: {e['msg']}")
    return "canvas does not match schema vwb.canvas/2:\n  " + "\n  ".join(lines)


@dataclass
class Problem:
    severity: str          # error | warn
    where: str
    why: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "where": self.where, "why": self.why}


def _frames(c: Canvas, step: int = 5) -> list[int]:
    return list(range(0, c.length, step)) + [c.length - 1]


def check(ws: Workspace, c: Canvas) -> list[Problem]:
    P: list[Problem] = []
    W, H = c.size
    prof = load_profile(c.profile)
    sz = prof["safe_zone"]
    safe = (sz["left"], sz["top"], W - sz["right"], H - sz["bottom"])
    media = {s.ref: s for s in ws.load().sources}
    for span in c.time_map:
        if span.media not in media:
            P.append(Problem("error", "time_map", f"unknown media {span.media}; known: {list(media)}"))
            continue
        n = probe(media[span.media].path, with_keyframes=False).n_frames
        if span.src_in < 0 or span.src_out > n:
            P.append(Problem("error", "time_map", f"span {span.src_in}-{span.src_out} is outside 0-{n}"))
    if c.length <= 0:
        P.append(Problem("error", "time_map", "the timeline is empty"))
        return P
    src = next(iter(media.values())) if media else None
    sw, sh = (probe(src.path, with_keyframes=False).width, probe(src.path, with_keyframes=False).height) if src else (0, 0)
    ids = [l.id for l in c.layers]
    for dup in {i for i in ids if ids.count(i) > 1}:
        P.append(Problem("error", dup, "layer ids must be unique"))
    deck = copydeck.load(ws)
    reg = Registry(ws.assets)
    fonts = font_library()
    for l in c.layers:
        if l.span and not (0 <= l.span[0] < l.span[1] <= c.length):
            P.append(Problem("error", l.id, f"span {l.span} is outside 0-{c.length}"))
        for prop in ("box", "crop", "opacity", "rotate", "draw", "fill"):
            v = getattr(l, prop, None)
            if is_keyed(v):
                fs = [k.f for k in v]
                if fs != sorted(fs) or len(set(fs)) != len(fs):
                    P.append(Problem("error", l.id, f"{prop} keyframes must have increasing f"))
        boxes = [b for b in all_values(getattr(l, "box", None)) if b is not None]
        for b in boxes:
            if len(b) != 4 or b[2] <= 0 or b[3] <= 0:
                P.append(Problem("error", l.id, f"box {b} must be [x, y, w, h] with positive size"))
        if isinstance(l, VideoLayer):
            if l.source not in media:
                P.append(Problem("error", l.id, f"unknown media {l.source}"))
            crops = [] if l.crop == "full" else all_values(l.crop)
            for cr in crops:
                if len(cr) != 4 or cr[2] <= 1 or cr[3] <= 1:
                    P.append(Problem("error", l.id, f"crop {cr} must be [x, y, w, h] in source pixels"))
                elif cr[0] < -2 or cr[1] < -2 or cr[0] + cr[2] > sw + 2 or cr[1] + cr[3] > sh + 2:
                    P.append(Problem("error", l.id, f"crop {[round(v) for v in cr]} leaves the {sw}x{sh} source frame"))
            if l.fit == "window" and l.crop != "full":
                worst = 0.0
                for f in _frames(c):
                    cr, bx = value_at(l.crop, f), value_at(l.box, f)
                    worst = max(worst, abs((cr[2] / cr[3]) / (bx[2] / bx[3]) - 1.0))
                if worst > 0.03:
                    P.append(Problem("error", l.id, f"crop and box aspects differ by {worst:.0%} (would squash); "
                                                    "match them or use fit=cover"))
        if isinstance(l, (ImageLayer, ClipLayer)):
            try:
                kind = reg.get(l.asset)["kind"]          # stills: image, mark.badge, mark.keyed, ...; clips: video
                if (kind == "video") != isinstance(l, ClipLayer):
                    want = "a video asset" if isinstance(l, ClipLayer) else "a still (not a video) asset"
                    P.append(Problem("error", l.id, f"{l.asset} is a {kind} asset; a {l.type} layer needs {want}"))
            except KeyError:
                P.append(Problem("error", l.id, f"unknown asset {l.asset}; make, extract or recover it first"))
        if isinstance(l, ClipLayer) and l.rate <= 0:
            P.append(Problem("error", l.id, "rate must be positive"))
        if isinstance(l, ShapeLayer) and l.shape == "path":
            ds = ([l.d] if l.d else []) + [pth.d for pth in (l.paths or [])]
            if not ds:
                P.append(Problem("error", l.id, "a path shape needs d or paths (SVG path data)"))
            elif any(re.search(r"[^MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]", d or "") for d in ds):
                P.append(Problem("error", l.id, "path data may hold only SVG path commands and numbers"))
        if isinstance(l, ShapeLayer) and l.shape == "svg":
            try:
                if reg.get(l.asset or "")["kind"] != "vector":
                    P.append(Problem("error", l.id, f"{l.asset} is not a vector asset (make one with asset_trace)"))
            except KeyError:
                P.append(Problem("error", l.id, f"unknown vector asset {l.asset}"))
        if isinstance(l, TextLayer) and l.copy_id not in deck:
            P.append(Problem("error", l.id, f"copy id {l.copy_id!r} is not in the copy deck; mint it with copy_add"))
        if isinstance(l, (TextLayer, CaptionsLayer)):
            fam = fonts.get(l.style.font)
            if fam is None:
                P.append(Problem("error", l.id, f"font {l.style.font!r} is not in the font library: {sorted(fonts)}"))
            elif l.style.weight not in fam["weights"]:
                P.append(Problem("error", l.id, f"{l.style.font} has weights {fam['weights']}, not {l.style.weight}"))
            elif l.style.italic and l.style.weight not in fam["italic"]:
                P.append(Problem("error", l.id, f"{l.style.font} italic exists only at weights {fam['italic']}"))
        if isinstance(l, (TextLayer, CaptionsLayer)) or (isinstance(l, ImageLayer) and l.role == "brand"):
            for b in boxes:
                if b[0] < safe[0] - 1 or b[1] < safe[1] - 1 or b[0] + b[2] > safe[2] + 1 or b[1] + b[3] > safe[3] + 1:
                    P.append(Problem("warn", l.id, f"box {[round(v) for v in b]} (x {b[0]:.0f}-{b[0] + b[2]:.0f}, y {b[1]:.0f}-"
                                                   f"{b[1] + b[3]:.0f}) leaves the safe zone x {safe[0]}-{safe[2]}, y {safe[1]}-"
                                                   f"{safe[3]}; text, marks and calls to action belong inside"))
                    break
    return P


# ---------------------------------------------------------------- expansion

def caption_pages(words: list[dict[str, Any]], max_chars: int, max_lines: int, max_dur: float = 2.6) -> list[list[list[dict[str, Any]]]]:
    pages: list[list[list[dict[str, Any]]]] = []
    page: list[list[dict[str, Any]]] = [[]]
    for w in words:
        line = page[-1]
        if line and sum(len(x["text"]) + 1 for x in line) + len(w["text"]) > max_chars:
            if len(page) < max_lines:
                page.append([])
            else:
                pages.append(page)
                page = [[]]
        page[-1].append(w)
        flat = [x for ln in page for x in ln]
        if re.search(r"[.!?]$", w["text"]) or (re.search(r"[,;:]$", w["text"]) and len(flat) >= 4) or w["t1"] - flat[0]["t0"] > max_dur:
            pages.append(page)
            page = [[]]
    if page[0]:
        pages.append(page)
    return pages


def output_words(store: IndexStore, c: Canvas) -> list[dict[str, Any]]:
    """Transcript words the output plays, with output frames (o0, o1) and seconds (t0, t1)."""
    fps = c.fps
    words, offset = [], 0
    for s in c.time_map:
        for w in store.all("SELECT * FROM words WHERE f0 >= ? AND f1 <= ? ORDER BY t0", (s.src_in, s.src_out + 1)):
            words.append({**w, "o0": offset + w["f0"] - s.src_in, "o1": offset + w["f1"] - s.src_in,
                          "t0": (offset + w["f0"] - s.src_in) / fps, "t1": (offset + w["f1"] - s.src_in) / fps})
        offset += s.src_out - s.src_in
    return words


def expand(ws: Workspace, c: Canvas) -> Canvas:
    """Fill the copy deck for text layers and derive caption pages from the transcript."""
    deck = copydeck.load(ws)
    store = IndexStore(ws.index / "index.sqlite")
    fps = c.fps
    copy = {}
    for l in c.layers:
        if isinstance(l, TextLayer):
            copy[l.copy_id] = deck[l.copy_id]
    words = output_words(store, c)
    claims = store.all("SELECT f0, f1 FROM hazards WHERE kind LIKE 'claim%'")
    for l in c.layers:
        if not isinstance(l, CaptionsLayer):
            continue
        lw = [w for w in words if l.span is None or (l.span[0] <= w["o0"] < l.span[1])]
        pages = []
        for n, page in enumerate(caption_pages(lw, l.max_chars, l.max_lines), start=1):
            flat = [w for ln in page for w in ln]
            cid = f"cap-{l.id}-{n:03d}"
            text = " ".join(w["text"] for w in flat)
            regulated = any(f0 < flat[-1]["f1"] and f1 > flat[0]["f0"] for f0, f1 in ((h["f0"], h["f1"]) for h in claims)) \
                or bool(CLAIM_HINT.search(text))
            copy[cid] = copydeck.caption_entry(cid, text, flat, regulated, c.time_map[0].media)
            pages.append({"copy_id": cid, "from": int(flat[0]["o0"]), "to": 0,
                          "lines": [[{"text": w["text"], "from": int(w["o0"]), "to": int(w["o1"])} for w in ln] for ln in page]})
        end = l.span[1] if l.span else c.length
        for i, pg in enumerate(pages):
            nxt = pages[i + 1]["from"] if i + 1 < len(pages) else end
            last = max(w["to"] for ln in pg["lines"] for w in ln)
            pg["to"] = int(min(nxt, max(last + int(0.6 * fps), pg["from"] + 6)))
        l.pages = pages
    c.copy_deck = list(copy.values())
    return c


# ---------------------------------------------------------------- render plan

@dataclass
class Step:
    kind: str                         # video | image | clip | plate
    layers: list[Any] = field(default_factory=list)
    plate_id: str | None = None
    props: dict[str, Any] | None = None
    start: int = 0                    # a plate covers output frames [start, end): only where its layers show
    end: int = 0


def plate_range(c: Canvas, layers: list[Any]) -> tuple[int, int]:
    if any(l.span is None for l in layers):
        return 0, c.length
    return max(0, min(l.span[0] for l in layers)), min(c.length, max(l.span[1] for l in layers))


def plate_props(c: Canvas, layers: list[Any], start: int = 0, end: int | None = None,
                registry: Registry | None = None) -> dict[str, Any]:
    """Props of one plate. Frames inside it are local; `offset` maps them back to output frames."""
    W, H = c.size
    end = c.length if end is None else end
    copy = c.copy_by_id()
    out = []
    for l in layers:
        d = l.model_dump(mode="json", exclude_none=True)
        if isinstance(l, TextLayer):
            d["text"] = copy[l.copy_id].text
        if isinstance(l, ShapeLayer) and l.shape == "svg" and registry is not None:
            vec = json.loads(Path(registry.get(l.asset)["path"]).read_text())   # the traced paths, inlined
            d["paths"], d["viewbox"] = vec["paths"], vec["viewbox"]
        out.append(d)
    return {"width": W, "height": H, "fps": c.fps, "durationInFrames": max(1, end - start), "offset": start,
            "total": c.length, "layers": out}


def plan(c: Canvas, registry: Registry | None = None) -> list[Step]:
    steps: list[Step] = []
    for l in c.layers:
        if isinstance(l, PLATE_TYPES):
            if steps and steps[-1].kind == "plate":
                steps[-1].layers.append(l)
            else:
                steps.append(Step("plate", [l]))
        elif isinstance(l, VideoLayer):
            steps.append(Step("video", [l]))
        elif isinstance(l, ClipLayer):
            steps.append(Step("clip", [l]))
        else:
            steps.append(Step("image", [l]))
    for s in steps:
        if s.kind == "plate":
            s.start, s.end = plate_range(c, s.layers)
            s.props = plate_props(c, s.layers, s.start, s.end, registry)
            s.plate_id = sha256_json({"pin": PLATE_PIN, "props": s.props})
    return steps
