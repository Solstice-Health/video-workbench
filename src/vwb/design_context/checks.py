"""Structural checks, never a claim of visual, medical, legal or PRC approval."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from vwb.canvas.anim import all_values
from vwb.canvas.copydeck import load as copy_deck
from vwb.design_context import store
from vwb.workspace.registry import Registry


def _text(value):
    # Preserve words, case, punctuation and order; HTML/line wrapping whitespace
    # is the only normalization. No paraphrase, summarizer or fuzzy matching.
    return " ".join(value.split())


_COLOR_FIELDS = {"color", "fill", "background", "accent_bar", "highlight", "muted", "tint", "gradient"}


def _color(value):
    """Canonical RGBA for supported CSS hex syntax; other syntax fails closed."""
    if not isinstance(value, str) or not re.fullmatch(r"#(?:[a-fA-F0-9]{3}|[a-fA-F0-9]{4}|[a-fA-F0-9]{6}|[a-fA-F0-9]{8})", value.strip()):
        raise ValueError(f"Unsupported color {str(value)[:100]!r}; use #RGB, #RGBA, #RRGGBB or #RRGGBBAA")
    value = value.strip()[1:]
    if len(value) in {3, 4}:
        value = "".join(c * 2 for c in value)
    return "#" + (value + "FF" if len(value) == 6 else value).upper()


def _color_values(value):
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            yield from _color_values(item["v"] if isinstance(item, dict) and "v" in item else item)
    else:
        yield _color(value)


def _colors(data, key=""):
    if key in _COLOR_FIELDS:
        yield from _color_values(data)
        return
    if isinstance(data, dict):
        for field, value in data.items():
            yield from _colors(value, field)
    elif isinstance(data, list):
        for value in data:
            yield from _colors(value, key)


def _identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _verified_hash(root, path, expected):
    """Read regular bytes under a trusted root without symlinks or digest caching.

    Reopening catches path replacement during hashing. The renderer must still
    consume the same files promptly: this does not pin its later file opens.
    """
    root, path = Path(root).absolute(), Path(path)
    if ".." in path.parts:
        raise ValueError("Media paths cannot contain traversal")
    try:
        relative = path.absolute().relative_to(root).as_posix()
    except ValueError:
        raise ValueError("Media path is outside its immutable media/asset root") from None
    digest, count = hashlib.sha256(), 0
    with store._open(root, relative) as stream:
        before = os.fstat(stream.fileno())
        # Bound reads by the initial size, including one byte to detect growth.
        while chunk := stream.read(min(1024**2, before.st_size - count + 1)):
            count += len(chunk)
            if count > before.st_size:
                raise ValueError("Media bytes changed during verification")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if _identity(before) != _identity(after) or count != before.st_size:
            raise ValueError("Media bytes changed during verification")
        with store._open(root, relative) as current:
            if _identity(os.fstat(current.fileno())) != _identity(after):
                raise ValueError("Media path changed during verification")
    actual = digest.hexdigest()
    if actual != expected:
        raise ValueError("Media bytes no longer match their frozen SHA-256")
    return actual


@dataclass(frozen=True)
class _MediaUse:
    label: str
    role: str | None
    kind: str
    reference: str


def _media_uses(canvas):
    timeline, offset = [], 0
    for span in canvas.time_map:
        end = offset + span.src_out - span.src_in
        if end > offset:
            timeline.append((span.media, offset, end))
        offset = end
    for layer in canvas.layers:
        start, end = layer.span or (0, canvas.length)
        if max(0, start) >= min(canvas.length, end):
            continue
        if layer.type in {"image", "clip"} or (layer.type == "shape" and layer.shape == "svg"):
            yield _MediaUse(f"Layer {layer.id}", layer.role, "asset", layer.asset)
        elif layer.type == "video" and any(
            ref == layer.source and max(start, lo) < min(end, hi) for ref, lo, hi in timeline
        ):
            yield _MediaUse(f"Layer {layer.id}", layer.role, "source", layer.source)
    # The audio pipeline opens each time-map source even without a video layer.
    # Role-scoped rules are visual-only; unscoped rules include these dependencies.
    for ref in dict.fromkeys(ref for ref, _, _ in timeline):
        yield _MediaUse(f"Timeline audio source {ref}", None, "source", ref)


def _verify_media(ws, uses):
    registry = Registry(ws.assets) if any(use.kind == "asset" for use in uses) else None
    sources = {s.ref: s for s in ws.load().sources} if any(use.kind == "source" for use in uses) else {}
    digests, findings, seen = {}, [], set()
    for use in uses:
        key = use.kind, use.reference
        if key in seen:
            continue
        seen.add(key)
        try:
            if use.kind == "asset":
                if not isinstance(use.reference, str) or not re.fullmatch(r"slot:[^@]+@[1-9][0-9]*", use.reference):
                    raise ValueError("Context-bound assets require an explicit slot:name@revision reference")
                asset = registry.get(use.reference)
                digests[key] = _verified_hash(ws.assets, asset["path"], asset["sha256"])
            else:
                if use.reference not in sources:
                    raise ValueError("Context-bound source media require an explicit, known media@revision reference")
                source = sources[use.reference]
                digests[key] = _verified_hash(ws.media, source.path, source.sha256)
        except (KeyError, OSError, ValueError) as exc:
            findings.append({"severity": "error", "where": "design_context:media",
                             "why": f"{use.label}: cannot verify frozen media: {exc}"})
    return digests, findings


def evaluate(ws, canvas, selected=None):
    selected = selected or canvas.design_context or (store.binding(ws, canvas.output) if not canvas.version else None)
    if not selected:
        return {"status": "not_configured", "findings": [], "binding": None}
    resolved = store.resolve(ws, selected["pack"], selected["facts"])
    findings = [{"severity": "error", "where": "design_context:" + row["rule"], "why": row["why"]}
                for row in resolved["errors"]]
    sources = {s["id"]: s for s in resolved["sources"]}
    deck = canvas.copy_by_id() if canvas.version else copy_deck(ws)
    uses = list(_media_uses(canvas))
    media_hashes, media_findings = _verify_media(ws, uses)
    findings.extend(media_findings)

    def issue(rule, message):
        findings.append({"severity": "error" if rule["level"] == "required" else "warn",
                         "where": "design_context:" + rule["id"], "why": message})

    for rule in resolved["rules"]:
        layers = [layer for layer in canvas.layers if not rule["roles"] or layer.role in rule["roles"]]
        kind = rule["kind"]
        if kind == "required_copy":
            matches = []
            for layer in layers:
                if layer.type != "text" or layer.copy_id not in deck:
                    continue
                text = deck[layer.copy_id].text
                if layer.style.uppercase:
                    text = text.upper()
                if _text(text) != _text(rule["exact_text"]):
                    continue
                # Until complete-copy display intervals are measured, reject
                # entrance/exit effects that truncate, clip, fade or move text.
                if layer.animate.enter != "none" or layer.animate.exit != "none":
                    continue
                span = layer.span or [0, canvas.length]
                if span[1] - span[0] < rule["min_frames"] or any(v <= 0 for v in all_values(layer.opacity)):
                    continue
                if any(b[2] <= 0 or b[3] <= 0 or b[0] >= canvas.size[0] or b[1] >= canvas.size[1]
                       or b[0] + b[2] <= 0 or b[1] + b[3] <= 0 for b in all_values(layer.box)):
                    continue
                matches.append(layer.id)
            if not matches:
                issue(rule, "Required exact source copy is missing, changed, animated, hidden or shorter than its configured frame span")
        elif kind in {"asset_allowlist", "required_asset"}:
            allowed = {sources[s]["sha256"] for s in rule["sources"]}
            found = set()
            for use in uses:
                if rule["roles"] and use.role not in rule["roles"]:
                    continue
                digest = media_hashes.get((use.kind, use.reference))
                if digest is None:
                    continue
                found.add(digest)
                if kind == "asset_allowlist" and digest not in allowed:
                    issue(rule, f"{use.label} uses media outside the selected asset library")
            if kind == "required_asset" and not (found & allowed):
                issue(rule, "The required library asset is absent")
        elif kind in {"fonts", "min_font_size"}:
            for layer in layers:
                if layer.type not in {"text", "captions"}:
                    continue
                if kind == "fonts" and layer.style.font not in rule["values"]:
                    issue(rule, f"Layer {layer.id} uses a font outside the configured list")
                if kind == "min_font_size":
                    floor = layer.style.min_size if layer.style.min_size is not None else layer.style.size / 2
                    if min(layer.style.size, floor) < rule["minimum"]:
                        issue(rule, f"Layer {layer.id} can shrink below the configured {rule['minimum']}px font floor")
        elif kind == "palette":
            allowed = {_color(v) for v in rule["values"]}
            for layer in layers:
                try:
                    outside = set(_colors(layer.model_dump())) - allowed
                except ValueError as exc:
                    issue(rule, f"Layer {layer.id}: {exc}")
                    continue
                if outside:
                    issue(rule, f"Layer {layer.id} declares colors outside the palette: {', '.join(sorted(outside))}")
        # Narrative guidance remains in resolve(), never auto-converted to a pass/fail rule.
    return {"status": "blocked" if any(f["severity"] == "error" for f in findings) else "structural_checks_passed",
            "binding": selected, "evaluated_as_of": resolved["as_of"], "findings": findings,
            "limits": "Checks inspect declared copy, assets and styles. Role-scoped media rules are visual-only; "
                      "unscoped media rules include timeline audio inputs. Hashes are freshly checked, but later renderer "
                      "file opens are not pinned by this check. Visibility, occlusion, rasterized content, clinical claims "
                      "and native After Effects projects require separate review; this is not approval."}


def assert_renderable(ws, canvas):
    """Recheck a bound canvas and its frozen media immediately before consumption.

    Committed canvases use their own saved binding/copy. Unbound canvases retain
    legacy behavior. This preflight does not eliminate subsequent render TOCTOU.
    """
    try:
        result = evaluate(ws, canvas)
    except (ValueError, OSError, KeyError) as exc:
        raise ValueError(f"Cannot verify design context before rendering: {exc}") from exc
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    if errors:
        raise ValueError("Design context prevents rendering:\n" + "\n".join(f"{f['where']}: {f['why']}" for f in errors))
    return result


def canvas_problems(ws, canvas):
    try:
        return evaluate(ws, canvas)["findings"]
    except (ValueError, OSError, KeyError) as exc:
        return [{"severity": "error", "where": "design_context", "why": str(exc)}]
