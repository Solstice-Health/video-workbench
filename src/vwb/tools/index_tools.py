"""Edit-index tools: build, overview (dossier), SQL, text search, transcript and shots."""

from __future__ import annotations

from typing import Any

from vwb.index.build import build_index
from vwb.index.store import IndexStore
from vwb.tools.base import Tool, ToolContext, ToolResult, schema
from vwb.util.timecode import fmt


def _store(ctx: ToolContext) -> IndexStore:
    return IndexStore(ctx.ws.index / "index.sqlite")


def index_build(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    summary = build_index(ctx.ws, force=bool(a.get("force")))
    return ToolResult.json(summary)


def index_overview(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    p = ctx.ws.index / "dossier.md"
    if not p.exists():
        return ToolResult.error("no index yet; call index_build")
    return ToolResult(text=p.read_text())


def index_query(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = _store(ctx).query_readonly(a["sql"], limit=int(a.get("limit", 200)))
    return ToolResult.json({"rows": rows, "n": len(rows)})


def index_search(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    q = a["text"].strip().lower()
    like = f"%{q}%"
    st = _store(ctx)
    hits = []
    for r in st.all("SELECT id, t0, t1, text, shot FROM sentences WHERE lower(text) LIKE ?", (like,)):
        hits.append({"kind": "speech", "id": f"s{r['id']}", "t": f"{fmt(r['t0'])}-{fmt(r['t1'])}", "shot": r["shot"], "text": r["text"]})
    for r in st.all("SELECT id, t0, t1, text, role, shot, x, y, w, h FROM text_tracks WHERE lower(text) LIKE ? OR lower(text2) LIKE ?", (like, like)):
        hits.append({"kind": "on_screen", "id": f"tt{r['id']}", "t": f"{fmt(r['t0'])}-{fmt(r['t1'])}", "shot": r["shot"],
                     "role": r["role"], "box": [round(r["x"]), round(r["y"]), round(r["w"]), round(r["h"])], "text": r["text"]})
    for r in st.all("SELECT id, kind, t0, t1, quote, producer FROM hazards WHERE lower(quote) LIKE ? OR lower(text) LIKE ?", (like, like)):
        hits.append({"kind": f"hazard.{r['kind']}", "id": f"hz{r['id']}", "t": f"{fmt(r['t0'])}-{fmt(r['t1'])}",
                     "producer": r["producer"], "text": (r["quote"] or "")[:240]})
    for r in st.all("SELECT id, t0, t1, title, hook, producer FROM moments WHERE lower(title) LIKE ? OR lower(hook) LIKE ?", (like, like)):
        hits.append({"kind": "moment", "id": f"m{r['id']}", "t": f"{fmt(r['t0'])}-{fmt(r['t1'])}", "producer": r["producer"],
                     "text": f"{r['title']} | {r['hook']}"})
    return ToolResult.json({"query": q, "hits": hits[: int(a.get("limit", 60))], "covered": "speech, on-screen text, hazards, moments"})


def index_transcript(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    st = _store(ctx)
    t0, t1 = float(a.get("t0", 0)), float(a.get("t1", 1e9))
    sents = st.all("SELECT * FROM sentences WHERE t1 > ? AND t0 < ? ORDER BY id", (t0, t1))
    out = []
    for s in sents:
        row = {"id": s["id"], "t0": round(s["t0"], 3), "t1": round(s["t1"], 3), "f0": s["f0"], "f1": s["f1"],
               "shot": s["shot"], "text": s["text"], "readers_agree": s["agree"]}
        if a.get("words") or s["agree"] < 0.999:
            ws = st.all("SELECT i, t0, t1, text, agree, alt FROM words WHERE sentence=? ORDER BY i", (s["id"],))
            if a.get("words"):
                row["words"] = [{"i": w["i"], "t0": round(w["t0"], 3), "t1": round(w["t1"], 3), "text": w["text"],
                                 **({"reader2": w["alt"]} if not w["agree"] else {})} for w in ws]
            else:
                row["disagreements"] = [{"text": w["text"], "reader2": w["alt"]} for w in ws if not w["agree"]]
        out.append(row)
    return ToolResult.json({"sentences": out, "fps": st.media()["fps"]})


def index_shots(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    st = _store(ctx)
    ids = a.get("shots")
    shots = st.all("SELECT * FROM shots ORDER BY id")
    if ids:
        shots = [s for s in shots if s["id"] in set(ids)]
    detail = bool(ids) or bool(a.get("detail"))
    out = []
    for s in shots:
        row = {"id": s["id"], "t": f"{fmt(s['t0'])}-{fmt(s['t1'])}", "f0": s["f0"], "f1": s["f1"], "kind": s["kind"],
               "title": s["title"], "description": s["description"], "has_face": bool(s["has_face"]), "n_text": s["n_text"]}
        if detail:
            row["text"] = [{"id": t["id"], "role": t["role"], "regulated": bool(t["regulated"]), "readers_agree": bool(t["agree"]),
                            "box": [round(t["x"]), round(t["y"]), round(t["w"]), round(t["h"])], "cap_h": t["cap_h"],
                            "t": f"{fmt(t['t0'])}-{fmt(t['t1'])}", "text": t["text"]}
                           for t in st.all("SELECT * FROM text_tracks WHERE shot=? ORDER BY y, x", (s["id"],))]
            row["blocks"] = [{"id": b["id"], "kind": b["kind"], "box": [round(b["x"]), round(b["y"]), round(b["w"]), round(b["h"])],
                              "text": (b["text"] or "")[:120]} for b in st.all("SELECT * FROM blocks WHERE shot=?", (s["id"],))]
        out.append(row)
    return ToolResult.json({"shots": out, "frame_size": [st.media()["width"], st.media()["height"]]})


TOOLS = [
    Tool("index_build", "Build or refresh the edit index for the request's source (eager facts and readings). "
         "Usually already built before you start.", schema({"force": {"type": "boolean"}}), index_build, "index.build", "index"),
    Tool("index_overview", "Read the dossier: a compact summary of the edit index (source facts, readings, hazards, "
         "marks, sections, shots, the two-reader transcript with sentence ids and times, proposed moments).",
         schema({}), index_overview, "tool", "index"),
    Tool("index_query", "Run one read-only SQL query on the index (tables: shots, sentences, words, text_tracks, faces, "
         "persons, saliency, blocks, silences, sections, moments, hazards, marks, readings, observations; view envelope).",
         schema({"sql": {"type": "string"}, "limit": {"type": "integer"}}, ["sql"]), index_query, "tool", "index"),
    Tool("index_search", "Search speech, on-screen text, hazards and moments for a phrase. Returns times, shots, boxes.",
         schema({"text": {"type": "string"}, "limit": {"type": "integer"}}, ["text"]), index_search, "tool", "index"),
    Tool("index_transcript", "Timed sentences between t0 and t1 seconds, with reader agreement; words=true adds word "
         "timings.", schema({"t0": {"type": "number"}, "t1": {"type": "number"}, "words": {"type": "boolean"}}),
         index_transcript, "tool", "index"),
    Tool("index_shots", "Shots with kind and on-screen title. Pass shot ids (or detail=true) for every text track "
         "(role, regulated, reader agreement, box, cap height) and layout block.",
         schema({"shots": {"type": "array", "items": {"type": "integer"}}, "detail": {"type": "boolean"}}),
         index_shots, "tool", "index"),
]
