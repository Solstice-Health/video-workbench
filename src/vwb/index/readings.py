"""Whole-video readings from two model families (spec D19). Readings are claims.

Family 1 (Gemini) watches the video with sound. Family 2 (GPT) reads the transcript, the
on-screen text tracks and contact sheets. Both return the same structure, so the dossier
can show where they disagree. Neither reading is evidence on its own: scripts decide.
"""

from __future__ import annotations

from typing import Any

from vwb.gateway import Gateway
from vwb.gateway import gemini as gem
from vwb.gateway import openai_api as oai
from vwb.util.timecode import fmt, parse

SHOT_KINDS = ["title_card", "section_card", "disclaimer", "slide_text", "slide_chart", "slide_presenter",
              "presenter", "footage", "logo_card", "references", "other"]
HAZARD_KINDS = ["usage_terms", "audience", "claim", "risk_statement", "isi", "job_code", "reference",
                "off_label", "comparative", "other"]

_T = {"type": "string", "description": "time as MM:SS.s from the start of the video"}

READING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "audience": {"type": "string", "description": "who the video says it is for"},
        "brand": {"type": "string"},
        "content_type": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "start": _T, "end": _T, "title": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["start", "end", "title"]}},
        "shots": {"type": "array", "items": {"type": "object", "properties": {
            "start": _T, "end": _T, "kind": {"type": "string", "enum": SHOT_KINDS},
            "title": {"type": "string"}, "description": {"type": "string"},
            "focus": {"type": "string", "description": "what a viewer should look at"},
            "presenter_visible": {"type": "boolean"}}, "required": ["start", "end", "kind"]}},
        "moments": {"type": "array", "items": {"type": "object", "properties": {
            "start": _T, "end": _T, "title": {"type": "string"}, "hook": {"type": "string"},
            "why": {"type": "string"}, "standalone": {"type": "boolean"},
            "score": {"type": "number", "description": "1-10, how well it works as a standalone short"}},
            "required": ["start", "end", "title", "score"]}},
        "hazards": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": HAZARD_KINDS}, "start": _T, "end": _T,
            "quote": {"type": "string"}, "why": {"type": "string"}}, "required": ["kind", "start", "quote"]}},
        "marks": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["logo", "wordmark", "icon", "badge"]}, "name": {"type": "string"},
            "start": _T, "end": _T, "where": {"type": "string"},
            "box_2d": {"type": "array", "items": {"type": "number"},
                       "description": "[ymin, xmin, ymax, xmax] normalised 0-1000 on a frame where it is largest"}},
            "required": ["kind", "name"]}},
        "style": {"type": "object", "properties": {
            "primary_color": {"type": "string"}, "accent_color": {"type": "string"}, "font_feel": {"type": "string"}}},
    },
    "required": ["summary", "sections", "shots", "moments", "hazards", "marks"],
}

VIDEO_PROMPT = """You are indexing a source video for a professional video editor. Watch the whole video with sound.
Return JSON that follows the schema. Be exact about times (MM:SS.s from the start of the video).

- sections: the video's own structure, in order, covering the whole video.
- shots: every distinct slide or shot in order, with its kind, its on-screen title (verbatim) and a one-line description.
- moments: 12 to 20 spans that could each stand alone as a short vertical video (15 to 60 s). Start and end on
  sentence boundaries of the narration. Say what the hook is and why it stands alone. Score 1-10.
- hazards: anything a regulated (pharma) reviewer must see: statements about who the video is for or how it may be
  used, claims and statistics, risk statements, important safety information, job codes, references and footnotes,
  comparative or off-label statements. Quote verbatim.
- marks: logos and brand marks, with where they appear and a box on the frame where each is largest.
- style: the dominant brand colours (hex) and the feel of the typography."""


def video_reading(gw: Gateway, video_path, duration: float) -> dict[str, Any]:
    f = gem.upload(gw, video_path)
    call = gem.generate(gw, "reading.video", [gem.video_part(f, fps=1.0), VIDEO_PROMPT],
                        purpose="index.reading.video", schema=READING_SCHEMA)
    data = call.data or {}
    data["_model"] = call.model
    return data


TEXT_INSTRUCTIONS = """You are the second, independent reader of a source video for a professional video editor.
You cannot watch the video. You get the timed transcript, the shot list with on-screen text, and contact sheets of
each shot. Return JSON that follows the schema, with times as MM:SS.s. Use the transcript times exactly.
moments: 12 to 20 spans that could each stand alone as a short vertical video (15 to 60 s), starting and ending on
sentence boundaries. hazards: quote verbatim anything a regulated (pharma) reviewer must see. Be precise and terse."""


def text_reading(gw: Gateway, transcript: str, shots_text: str, sheets: list[bytes]) -> dict[str, Any]:
    parts = [oai.text_input("TRANSCRIPT (sentence id, start-end, text):\n" + transcript),
             oai.text_input("SHOTS AND ON-SCREEN TEXT:\n" + shots_text)]
    parts += [oai.image_input(png, mime="image/jpeg", detail="high") for png in sheets]
    call = oai.respond(gw, "reading.text", [oai.user_message(*parts)], purpose="index.reading.text",
                       instructions=TEXT_INSTRUCTIONS, schema=READING_SCHEMA, schema_name="video_reading")
    data = call.data or {}
    data["_model"] = call.model
    return data


def to_seconds(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return parse(str(value))
    except ValueError:
        return default


def describe_disagreements(a: dict[str, Any], b: dict[str, Any], tol: float = 2.0) -> list[str]:
    """Where the two readings disagree on structure: section starts and shot kinds."""
    notes = []
    sa = sorted(to_seconds(s.get("start"), 0.0) for s in a.get("sections", []))
    sb = sorted(to_seconds(s.get("start"), 0.0) for s in b.get("sections", []))
    only_a = [t for t in sa if all(abs(t - u) > tol for u in sb)]
    only_b = [t for t in sb if all(abs(t - u) > tol for u in sa)]
    if only_a:
        notes.append("section starts only in reading A: " + ", ".join(fmt(t, ms=False) for t in only_a))
    if only_b:
        notes.append("section starts only in reading B: " + ", ".join(fmt(t, ms=False) for t in only_b))
    return notes
