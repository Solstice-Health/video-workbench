"""The copy deck: every typeset string, minted by code with its provenance (spec D21).

Agents never type a string into a canvas. They mint copy from on-screen text (two OCR
readers), from the transcript (two ASR readers), or as authored text (one reader: the
author, so it is always unverified). The deck lives outside the agents' file area
(copy/deck.json), and a canvas that cites a string not in the deck does not validate.
"""

from __future__ import annotations

import re
from typing import Any

from filelock import FileLock

from vwb.canvas.model import CopyEntry
from vwb.index.ocr import CLAIM_HINT
from vwb.index.paths import SourceIndexes
from vwb.index.paths import source as selected_source
from vwb.index.store import IndexStore
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace

_SUP = re.compile(r"(?<=[A-Za-z%)\]])((?:\d{1,2}(?:[,–-]\d{1,2})*)|[*†‡§]+)(?=$|[\s.,;:])")


def mark_superscripts(text: str) -> str:
    """'years1,2' -> 'years^{1,2}': reference markers glued to a word are superscripts on slides."""
    return _SUP.sub(lambda m: "^{" + m.group(1) + "}", text)


def plain(text: str) -> str:
    return re.sub(r"\^\{([^}]*)\}", r"\1", text)


def _path(ws: Workspace):
    p = ws.root / "copy" / "deck.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load(ws: Workspace) -> dict[str, CopyEntry]:
    return {k: CopyEntry.model_validate(v) for k, v in (read_json(_path(ws), {}) or {}).items()}


def _seal(entry: CopyEntry) -> CopyEntry:
    entry.sha256 = sha256_json({k: v for k, v in entry.model_dump().items() if k != "sha256"})
    return entry


def _save(ws: Workspace, entry: CopyEntry) -> CopyEntry:
    with FileLock(str(_path(ws)) + ".lock"):
        deck = read_json(_path(ws), {}) or {}
        if entry.id in deck and deck[entry.id]["sha256"] != entry.sha256:
            raise ValueError(f"copy id {entry.id!r} already holds different text; mint under a new id")
        deck[entry.id] = entry.model_dump()
        write_json(_path(ws), deck)
    return entry


def _regulated(store: IndexStore | None, text: str, f0: int | None = None, f1: int | None = None) -> bool:
    if CLAIM_HINT.search(text):
        return True
    if store is not None and f0 is not None and f1 is not None:
        hit = store.one("SELECT COUNT(*) AS n FROM hazards WHERE kind LIKE 'claim%' AND f0 < ? AND f1 > ?", (f1, f0))
        return bool(hit and hit["n"])
    return False


def mint(ws: Workspace, *, copy_id: str, role: str, source: str, shot: int | None = None,
         tracks: list[int] | None = None, sentences: list[int] | None = None, text: str | None = None,
         note: str | None = None, media_id: str | None = None) -> CopyEntry:
    store = None
    req = ws.load()
    if not req.sources and source != "authored":
        raise ValueError("No source video is attached. Use authored copy with a note citing the Figma file/node or brief; it remains unverified.")
    media = selected_source(ws, media_id, require_explicit=True).ref if source != "authored" else None
    if media:
        store = SourceIndexes(ws).for_media(media)
    if source == "shot_title":
        rows = store.all("SELECT * FROM text_tracks WHERE shot=? AND role='title' ORDER BY y, x", (shot,))
        if not rows:
            raise ValueError(f"shot {shot} has no on-screen title; use source='ocr' with track ids")
        biggest = max(r["h"] for r in rows)
        rows = [r for r in rows if r["h"] >= 0.7 * biggest]
        tracks = [r["id"] for r in rows]
        source = "ocr"
    if source == "ocr":
        rows = [store.one("SELECT * FROM text_tracks WHERE id=?", (t,)) for t in tracks or []]
        if not rows or any(r is None for r in rows):
            raise ValueError("unknown text track id(s)")
        seen, ordered = set(), []
        for r in sorted(rows, key=lambda r: (round(r["y"] / 8), r["x"])):
            if r["text"] not in seen:
                seen.add(r["text"])
                ordered.append(r)
        raw = " ".join(r["text"] for r in ordered)
        entry = CopyEntry(id=copy_id, text=mark_superscripts(raw), role=role,
                          source={"kind": "ocr", "tracks": [r["id"] for r in ordered], "media": media},
                          readers={"ocr.reader1": raw, "ocr.reader2": " ".join(r["text2"] or "" for r in ordered)},
                          agree=all(bool(r["agree"]) for r in ordered),
                          regulated=any(bool(r["regulated"]) for r in ordered) or _regulated(store, raw))
    elif source == "transcript":
        if not sentences:
            raise ValueError("give sentence ids")
        words = store.all(f"SELECT * FROM words WHERE sentence IN ({','.join('?' * len(sentences))}) ORDER BY i",
                          tuple(sentences))
        if not words:
            raise ValueError("No transcript words for those sentence IDs in the selected source.")
        raw = " ".join(w["text"] for w in words)
        alts = [f"{w['text']}→{w['alt']}" for w in words if not w["agree"] and w["alt"] is not None]
        entry = CopyEntry(id=copy_id, text=raw, role=role,
                          source={"kind": "asr", "sentences": sentences, "media": media},
                          readers={"asr.reader1+2": raw, **({"disagreements": "; ".join(alts)} if alts else {})},
                          agree=all(bool(w["agree"]) for w in words),
                          regulated=_regulated(store, raw, words[0]["f0"], words[-1]["f1"]))
    elif source == "authored":
        if not text or not text.strip():
            raise ValueError("authored copy needs text")
        entry = CopyEntry(id=copy_id, text=text.strip(), role=role,
                          source={"kind": "authored", **({"note": note.strip()} if note else {})},
                          readers={"author": text.strip()}, agree=False,
                          regulated=role != "label" or bool(re.search(r"\d", text)) or _regulated(store, text))
    else:
        raise ValueError("source must be shot_title, ocr, transcript or authored")
    return _save(ws, _seal(entry))


def caption_entry(copy_id: str, text: str, words: list[dict[str, Any]], regulated: bool, media: str) -> CopyEntry:
    """Derived caption copy (one per caption page), minted by the compiler, never by an agent."""
    alts = [f"{w['text']}→{w['alt']}" for w in words if not w["agree"] and w.get("alt") is not None]
    return _seal(CopyEntry(id=copy_id, text=text, role="caption",
                           source={"kind": "asr", "words": [w["i"] for w in words], "media": media},
                           readers={"asr.reader1+2": text, **({"disagreements": "; ".join(alts)} if alts else {})},
                           agree=all(bool(w["agree"]) for w in words), regulated=regulated))
