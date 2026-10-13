"""Build runs/<request>/review/index.html from records only (canvas, receipts, verification,
ledger, asks). Videos play in place; every flag shows its id so a person can act on it."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from vwb.ask.broker import Broker
from vwb.canvas.store import CanvasStore
from vwb.index.store import IndexStore
from vwb.render.pipeline import render_dir
from vwb.util.jsonio import read_json
from vwb.util.timecode import fmt
from vwb.workspace import Workspace, flags
from vwb.workspace.ledger import Ledger

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#141821;--muted:#5b6475;--rule:#dde2ec;--red:#c62839;--red-s:#fde6ea;
--yellow:#9a6400;--yellow-s:#fff2d6;--green:#16794a;--green-s:#ddf5e8;--info:#2a5fb0;--info-s:#e3edfb;--accent:#2348d6}
@media (prefers-color-scheme:dark){:root{--bg:#0e1117;--card:#161b24;--ink:#e7ebf3;--muted:#98a2b3;--rule:#2a3242;
--red:#ff8597;--red-s:#3a1820;--yellow:#f3c056;--yellow-s:#33280f;--green:#5fd59a;--green-s:#11301f;--info:#8ab4ff;
--info-s:#17263d;--accent:#8ea8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Inter,sans-serif}main{max-width:1320px;margin:0 auto;padding:24px 16px 80px}
h1{font-size:1.6rem;margin:0 0 6px}h2{font-size:1.15rem;margin:34px 0 10px}.muted{color:var(--muted)}
code{font:12.5px ui-monospace,Menlo,monospace;background:var(--rule);padding:1px 5px;border-radius:4px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:12px;padding:12px;min-width:0}
video{width:100%;aspect-ratio:9/16;background:#000;border-radius:8px;display:block}
.badge{display:inline-block;font:600 11px/1 ui-monospace,Menlo,monospace;padding:5px 8px;border-radius:6px;letter-spacing:.04em}
.RED{background:var(--red-s);color:var(--red)}.YELLOW{background:var(--yellow-s);color:var(--yellow)}
.GREEN{background:var(--green-s);color:var(--green)}.BLOCK{background:var(--red-s);color:var(--red)}
.HOLD{background:var(--yellow-s);color:var(--yellow)}.WARN{background:var(--info-s);color:var(--info)}
.INFO{background:var(--rule);color:var(--muted)}
ul.flags{list-style:none;padding:0;margin:8px 0}ul.flags li{margin:6px 0;font-size:13px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
.title{font-weight:650;font-size:14px;margin:8px 0 2px}details{margin-top:6px}summary{cursor:pointer;color:var(--accent);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border-top:1px solid var(--rule);padding:6px 8px;text-align:left;vertical-align:top}
.ask{border-left:4px solid var(--accent)}.hazard{border-left:4px solid var(--red)}
"""

SEV = {"BLOCK": 0, "HOLD": 1, "WARN": 2, "INFO": 3}


def _e(x: Any) -> str:
    return html.escape(str(x))


def build(ws: Workspace) -> Path:
    req = ws.load()
    store = CanvasStore(ws.canvas)
    ledger = Ledger(ws.ledger / "ledger.jsonl")
    out_dir = ws.review
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for out in sorted(store.outputs()):
        v = store.head(out)
        c = store.get(out, v)
        rec = read_json(render_dir(ws, out, v) / "receipt.json", {})
        ver = read_json(ws.qc / out / f"v{v}" / "verification.json", {})
        fl = [f for f in flags.current_flags(ledger, output=out, version=v) if f["status"] == "open" and f.get("output") == out]
        rows.append((out, v, c, rec, ver, sorted(fl, key=lambda f: (SEV[f["severity"]], f["kind"]))))
    counts = {k: sum(1 for r in rows if r[4].get("triage") == k) for k in ("RED", "YELLOW", "GREEN")}
    P: list[str] = []
    a = P.append
    a(f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
      f"<title>Review {_e(req.id)}</title><style>{CSS}</style></head><body><main>")
    a(f"<p class=muted>Video Workbench review page · request <code>{_e(req.id)}</code></p>")
    a(f"<h1>{_e(req.message[:160])}</h1>")
    src = req.sources[0] if req.sources else None
    if src:
        a(f"<p class=muted>Source <code>{_e(Path(src.original_path).name)}</code> ({_e(src.ref)}). "
          f"{len(rows)} output(s): {counts['RED']} red, {counts['YELLOW']} yellow, {counts['GREEN']} green. "
          "Nothing here is accepted until a person signs <code>vwb accept</code>; this is not PRC or MLR approval.</p>")

    tickets = Broker(ws).tickets()
    if tickets:
        a("<h2>Questions for you</h2><div class=cards>")
        for t in tickets:
            opts = "".join(f"<li><b>{_e(o['id'])}</b>: {_e(o.get('label', ''))}"
                           + (f" <span class=muted>({_e(o.get('consequence', ''))})</span>" if o.get("consequence") else "")
                           + "</li>" for o in t.get("options", []))
            ans = t.get("answer") or {}
            a(f"<div class='card ask'><div class=row><span class='badge INFO'>{_e(t['status'])}</span>"
              f"<code>{_e(t['id'])}</code></div><div class=title>{_e(t['question'])}</div>"
              f"<p class=muted>{_e(t.get('why', ''))}</p><ul>{opts}</ul>"
              f"<p>Recommended: <b>{_e(t['recommended'])}</b>{' · answer: <b>' + _e(ans.get('option')) + '</b>' if ans else ''}</p>"
              f"<p class=muted>Answer: <code>vwb answer {_e(req.id)} {_e(t['id'])} &lt;option&gt;</code></p></div>")
        a("</div>")

    idx = ws.index / "index.sqlite"
    if idx.exists():
        hz = IndexStore(idx).all("SELECT * FROM hazards WHERE status='fact' AND kind IN ('usage_terms','job_code','isi') ORDER BY t0")
        if hz:
            a("<h2>What the source says about its own use</h2><div class=cards>")
            seen = set()
            for h in hz:
                q = h["quote"] or h["text"]
                if q in seen:
                    continue
                seen.add(q)
                a(f"<div class='card hazard'><div class=row><span class='badge BLOCK'>{_e(h['kind'])}</span>"
                  f"<span class=muted>{fmt(h['t0'])}–{fmt(h['t1'])}</span></div><p>“{_e(q[:500])}”</p></div>")
            a("</div>")

    a("<h2>Outputs</h2><div class=cards>")
    for out, v, c, rec, ver, fl in rows:
        tri = ver.get("triage", "NOT SUBMITTED")
        video = Path(rec["file"]).resolve().relative_to(ws.root.resolve()) if rec.get("file") else None
        title = next((x.text for x in c.copy_deck if x.role == "title"), "") or c.intent
        spans = ", ".join(f"{fmt(s.src_in / c.fps)}–{fmt(s.src_out / c.fps)}" for s in c.time_map)
        a("<div class=card>")
        if video:
            a(f"<video controls preload=metadata src='../{_e(video)}'></video>")
        a(f"<div class=row><span class='badge {_e(tri)}'>{_e(tri)}</span><b>{_e(out)}</b><span class=muted>v{v} · "
          f"{c.length / c.fps:.1f}s · source {spans}</span></div>")
        if title:
            a(f"<div class=title>{_e(title.replace('^{', '').replace('}', ''))}</div>")
        a("<ul class=flags>")
        for f in fl:
            a(f"<li><span class='badge {f['severity']}'>{f['severity']}</span> <b>{_e(f['kind'])}</b> "
              f"{_e(f['summary'][:220])} <code>{_e(f['id'])}</code></li>")
        a("</ul>")
        qrel = f"../qc/{out}/v{v}"
        a(f"<details><summary>Records</summary><p><a href='{qrel}/delivery_note.md'>delivery note</a> · "
          f"<a href='{qrel}/cue_sheet.json'>cue sheet</a> · <a href='{qrel}/verification.json'>verification</a> · "
          f"<a href='{qrel}/manifest.json'>signed manifest</a> · <a href='../canvas/{out}/v{v}.json'>canvas file</a></p>"
          f"<p class=muted>Act: <code>vwb ack {_e(req.id)} &lt;flag id&gt;</code> · "
          f"<code>vwb accept {_e(req.id)} {_e(out)}</code></p></details>")
        caps = [x for x in c.copy_deck if x.role == "caption"]
        if caps:
            a("<details><summary>Typeset copy</summary><table><tr><th>id</th><th>text</th><th>readers</th></tr>")
            for x in [y for y in c.copy_deck if y.role != "caption"] + caps:
                a(f"<tr><td><code>{_e(x.id)}</code></td><td>{_e(x.text)}</td><td>{'agree' if x.agree else 'check'}</td></tr>")
            a("</table></details>")
        a("</div>")
    a("</div>")
    req_flags = [f for f in flags.current_flags(ledger) if f.get("output") is None and f["status"] == "open"]
    if req_flags:
        a("<h2>Request-level notices</h2><ul class=flags>")
        for f in sorted(req_flags, key=lambda f: SEV[f["severity"]]):
            a(f"<li><span class='badge {f['severity']}'>{f['severity']}</span> <b>{_e(f['kind'])}</b> {_e(f['summary'][:240])}</li>")
        a("</ul>")
    a("</main></body></html>")
    page = out_dir / "index.html"
    page.write_text("\n".join(P))
    return page
