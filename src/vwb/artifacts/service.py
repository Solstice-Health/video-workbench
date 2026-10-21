"""Versioned HTML artifacts. Iframe messages stage feedback; only the host submits it."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock

from vwb.agents.events import emit
from vwb.util.jsonio import read_json, write_json
from vwb.web.workspace_files import open_file
from vwb.workspace import Workspace
from vwb.workspace.request import now_iso

MAX_HTML_BYTES = 2_000_000
MAX_ITEMS = 500
MAX_ASSETS = 1000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def ident(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Use an identifier of 1–96 letters, digits, dots, underscores or hyphens.")
    return value


def _text(value: Any, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1–{maximum} characters.")
    return value.strip()


def _root(ws: Workspace) -> Path:
    return ws.memory / "artifacts"


def get(ws: Workspace, artifact_id: str, revision: int | None = None) -> dict:
    folder = _root(ws) / ident(artifact_id)
    if revision is None:
        revision = read_json(folder / "latest.json", {}).get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise FileNotFoundError("This artifact revision was not found.")
    row = read_json(folder / f"v{revision:06d}" / "manifest.json")
    if not row:
        raise FileNotFoundError("This artifact revision was not found.")
    return row


def listing(ws: Workspace) -> list[dict]:
    result = [get(ws, path.parent.name) for path in sorted(_root(ws).glob("*/latest.json"))]
    return sorted(result, key=lambda row: row["created_at"], reverse=True)


def html_path(ws: Workspace, artifact_id: str, revision: int) -> Path:
    row = get(ws, artifact_id, revision)
    return _root(ws) / row["id"] / f"v{row['revision']:06d}" / "index.html"


def publish(ws: Workspace, *, html: str, title: str, actor: str, artifact_id: str | None = None,
            items: list[dict] | None = None, assets: list[str] | None = None) -> dict:
    title = _text(title, 160, "Title")
    artifact_id = ident(artifact_id or f"artifact-{uuid.uuid4().hex[:12]}")
    if not isinstance(html, str) or not html.endswith((".html", ".htm")):
        raise ValueError("html_path must name an HTML file inside work/.")
    source = html if html.startswith("work/") else f"work/{html}"
    with open_file(ws, source) as fh:
        content = fh.read(MAX_HTML_BYTES + 1)
    if not content or len(content) > MAX_HTML_BYTES:
        raise ValueError("Artifact HTML must be between 1 byte and 2 MB.")
    content.decode("utf-8")
    if not isinstance(items or [], list) or len(items or []) > MAX_ITEMS:
        raise ValueError("An artifact supports at most 500 review items.")
    if not isinstance(assets or [], list) or len(assets or []) > MAX_ASSETS:
        raise ValueError("An artifact supports at most 1,000 explicit assets.")
    clean_items, seen, paths = [], set(), list(assets or [])
    for item in items or []:
        if not isinstance(item, dict):
            raise ValueError("Each review item must be an object.")
        item_id = ident(item.get("id"))
        if item_id in seen:
            raise ValueError("Review item identifiers must be unique.")
        seen.add(item_id)
        row = {"id": item_id, "label": _text(item.get("label", item_id), 200, "Item label")}
        if item.get("asset_path"):
            row["asset_path"] = item["asset_path"]
            paths.append(item["asset_path"])
        clean_items.append(row)
    if any(not isinstance(path, str) for path in paths):
        raise ValueError("Asset paths must be strings relative to the request workspace.")
    paths = list(dict.fromkeys(paths))
    if len(paths) > MAX_ASSETS:
        raise ValueError("An artifact supports at most 1,000 explicit assets.")
    clean_assets = []
    for path in paths:
        with open_file(ws, path) as fh:
            stat = os.fstat(fh.fileno())
            clean_assets.append({"path": path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                 "inode": stat.st_ino})
    folder = _root(ws) / artifact_id
    folder.mkdir(parents=True, exist_ok=True)
    with FileLock(str(folder / "publish.lock")):
        previous = read_json(folder / "latest.json", {})
        revision = previous.get("revision", 0) + 1
        target = folder / f"v{revision:06d}"
        target.mkdir()
        (target / "index.html").write_bytes(content)
        row = {"id": artifact_id, "revision": revision, "title": title, "source_path": source,
               "sha256": hashlib.sha256(content).hexdigest(), "items": clean_items, "assets": clean_assets,
               "created_at": now_iso(), "actor": actor}
        write_json(target / "manifest.json", row)
        write_json(folder / "latest.json", {"revision": revision})
    emit(ws, "artifact.published", {"id": artifact_id, "revision": revision, "title": title}, actor=actor)
    return row


def public(row: dict) -> dict:
    return {**row, "assets": [{"path": asset["path"], "size": asset["size"]} for asset in row["assets"]]}


def check_asset(handle, asset: dict) -> None:
    stat = os.fstat(handle.fileno())
    if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (asset["size"], asset["mtime_ns"], asset["inode"]):
        raise ValueError("This artifact asset changed. Ask the agent to publish a fresh revision.")


def selection(row: dict, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Select an artifact item first.")
    item_id = payload.get("item_id")
    if item_id is not None and item_id not in {item["id"] for item in row["items"]}:
        raise ValueError("That review item does not belong to this artifact revision.")
    out = {"item_id": item_id}
    region = payload.get("region")
    if region is not None:
        if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
            raise ValueError("A region requires normalized x, y, width and height.")
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in region.values()):
            raise ValueError("Region coordinates must be finite numbers.")
        x, y, width, height = (region[k] for k in ("x", "y", "width", "height"))
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
            raise ValueError("A region must fit within normalized bounds 0–1.")
        if item_id is None:
            raise ValueError("A region must identify its review item.")
        out["region"] = region
    time_s = payload.get("time_s")
    if time_s is not None:
        if isinstance(time_s, bool) or not isinstance(time_s, (int, float)) or not math.isfinite(time_s) or not 0 <= time_s <= 86400:
            raise ValueError("Time must be a finite number between 0 and 86,400 seconds.")
        if item_id is None:
            raise ValueError("A time must identify its review item.")
        out["time_s"] = time_s
    return out


def feedback(ws: Workspace, artifact_id: str, *, after: int = 0, limit: int = 100) -> dict:
    get(ws, artifact_id)
    rows = read_json(_root(ws) / artifact_id / "feedback.json", [])
    limit = max(1, min(int(limit), 200))
    page = [row for row in rows if row["seq"] > max(0, after)][:limit]
    return {"feedback": page, "cursor": page[-1]["seq"] if page else after,
            "has_more": bool(page and page[-1]["seq"] < len(rows))}


def submit_feedback(ws: Workspace, artifact_id: str, body: dict, enqueue) -> dict:
    """Called by the trusted host POST only. Retrying a submission never queues twice."""
    revision = body.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Feedback must identify the displayed artifact revision.")
    row = get(ws, artifact_id, revision)
    selected = selection(row, body)
    text = _text(body.get("text"), 8000, "Feedback")
    mode = body.get("mode", "queue")
    if mode not in {"queue", "now"}:
        raise ValueError("Choose queue or now for feedback delivery.")
    submission_id = ident(body.get("submission_id"))
    signature = hashlib.sha256(json.dumps({"revision": revision, **selected, "text": text, "mode": mode},
                                         sort_keys=True).encode()).hexdigest()
    folder = _root(ws) / row["id"]
    with FileLock(str(folder / "feedback.lock")):
        rows = read_json(folder / "feedback.json", [])
        existing = next((entry for entry in rows if entry["id"] == submission_id), None)
        if existing:
            if existing.get("submission_hash") != signature:
                raise ValueError("This feedback submission identifier was already used for different feedback.")
            if existing.get("delivery") == "queued":
                return existing
        item = next((item for item in row["items"] if item["id"] == selected["item_id"]), None)
        target = item["label"] if item else "Whole artifact"
        context = {"artifact_id": row["id"], "revision": row["revision"], **selected}
        if item and item.get("asset_path"):
            context["asset_path"] = item["asset_path"]
            pinned = next(asset for asset in row["assets"] if asset["path"] == item["asset_path"])
            context["asset_version"] = {key: pinned[key] for key in ("size", "mtime_ns")}
            if not existing:
                with open_file(ws, item["asset_path"]) as handle:
                    check_asset(handle, pinned)
        message = (f"Feedback on {row['title']} (revision {row['revision']}), {target}:\n{text}\n\n"
                   f"Artifact selection: {json.dumps(context, ensure_ascii=False)}")
        # Persist before enqueue. The caller supplies a stable queue idempotency key;
        # a crash on either side is reconciled by retrying this same submission.
        result = existing or {"seq": len(rows) + 1, "id": submission_id, "created_at": now_iso(), **context,
                              "text": text, "mode": mode, "delivery": "pending", "submission_hash": signature}
        if not existing:
            rows.append(result)
            write_json(folder / "feedback.json", rows)
        job = enqueue(message, mode)
        result.update(delivery="queued", job_id=job["id"])
        write_json(folder / "feedback.json", rows)
    emit(ws, "artifact.feedback", {"artifact_id": artifact_id, "revision": row["revision"], "feedback_id": submission_id,
                                   "item_id": selected["item_id"], "job_id": job["id"]}, actor="human")
    return result


# No credential, host state or chat mutation API is exposed to arbitrary authored HTML.
BRIDGE_SCRIPT = r"""
(() => {
  const manifest = __VWB_MANIFEST__;
  const assets = __VWB_ASSETS__;
  Object.defineProperty(window, 'VWB', {value: Object.freeze({
    artifact: Object.freeze(manifest), assets: Object.freeze(assets),
    select: selection => parent.postMessage({type:'vwb-artifact-select', artifact_id:manifest.id,
      revision:manifest.revision, selection}, '*')
  })});
  addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-vwb-asset]').forEach(node => {
      const url = assets[node.dataset.vwbAsset]; if (url) node.src = url;
    });
    document.querySelectorAll('[data-vwb-item]').forEach(node => node.addEventListener('click', () => {
      const media = node.matches('video,audio') ? node : node.querySelector('video,audio');
      VWB.select({item_id:node.dataset.vwbItem, ...(media ? {time_s:media.currentTime} : {})});
    }));
  });
})();
"""


def render_html(ws: Workspace, row: dict, asset_urls: dict) -> bytes:
    def js(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    bridge = BRIDGE_SCRIPT.replace("__VWB_MANIFEST__", js({k: row[k] for k in ("id", "revision", "title", "items")}))
    bridge = bridge.replace("__VWB_ASSETS__", js(asset_urls))
    content = html_path(ws, row["id"], row["revision"]).read_text()
    return ("<!doctype html><script>" + bridge + "</script>\n" + content).encode()
