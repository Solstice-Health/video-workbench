"""Files that travel with a delivered output: captions, a description draft and a thumbnail.

  captions.srt     the two-reader transcript of what the output plays, paged for reading
  description.md   a draft for the platform's description field: the title and the references the
                   output's span shows (source text only; it is reviewed like any other copy)
  thumbnail.jpg    the frame where the output's first title is fully shown
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from vwb.canvas.compile import caption_pages, output_words
from vwb.canvas.model import Canvas, TextLayer
from vwb.index.store import IndexStore
from vwb.media.decode import read_frames
from vwb.workspace import Workspace


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def captions_srt(store: IndexStore, canvas: Canvas) -> str:
    words = output_words(store, canvas)
    blocks = []
    for n, page in enumerate(caption_pages(words, max_chars=42, max_lines=2, max_dur=4.0), start=1):
        flat = [w for ln in page for w in ln]
        lines = "\n".join(" ".join(w["text"] for w in ln) for ln in page if ln)
        blocks.append(f"{n}\n{_ts(flat[0]['t0'])} --> {_ts(flat[-1]['t1'] + 0.15)}\n{lines}\n")
    return "\n".join(blocks)


def description(ws: Workspace, store: IndexStore, canvas: Canvas) -> str:
    copy = canvas.copy_by_id()
    titles = [copy[l.copy_id].text for l in canvas.layers if isinstance(l, TextLayer) and l.copy_id in copy
              and copy[l.copy_id].role == "title"]
    refs: list[str] = []
    for s in canvas.time_map:                       # references shown on the slides the output plays
        for t in store.all("SELECT text FROM text_tracks WHERE role IN ('reference', 'footnote') AND f1 > ? AND f0 < ? "
                           "AND agree=1 ORDER BY f0, y", (s.src_in, s.src_out)):
            if t["text"] not in refs:
                refs.append(t["text"])
    shown_refs = [c.text for c in canvas.copy_deck if c.role == "reference"]
    lines = ["<!-- Draft description for review. Recorded copy only; confirm before publishing. -->", ""]
    if titles:
        lines += [titles[0].replace("^{", "").replace("}", ""), ""]
    if shown_refs or refs:
        lines.append("References and notes")
        for r in shown_refs + [r for r in refs if r not in shown_refs]:
            lines.append(f"- {r.replace('^{', '').replace('}', '')}")
        lines.append("")
    req = ws.load()
    if canvas.time_map and req.sources:
        lines.append(f"Derived from {req.source().original_path.split('/')[-1]} ({req.source().ref}).")
    else:
        lines.append("Created from the brief and design references, without a source-video timeline. Review imported evidence and asset provenance before publishing.")
    return "\n".join(lines) + "\n"


def thumbnail(mp4: Path, canvas: Canvas, out: Path) -> Path:
    first_title = min((l.span[0] if l.span else 0 for l in canvas.layers if isinstance(l, TextLayer)), default=0)
    f = min(canvas.length - 1, first_title + int(1.0 * canvas.fps))
    rgb = read_frames(mp4, [f])[f]
    Image.fromarray(rgb).save(out, format="JPEG", quality=90)
    return out


def write(ws: Workspace, canvas: Canvas, mp4: Path, qdir: Path) -> dict[str, Path]:
    store = IndexStore(ws.index / "index.sqlite")
    files = {"captions": qdir / "captions.srt", "description": qdir / "description.md", "thumbnail": qdir / "thumbnail.jpg"}
    files["captions"].write_text(captions_srt(store, canvas))
    files["description"].write_text(description(ws, store, canvas))
    thumbnail(mp4, canvas, files["thumbnail"])
    return files
