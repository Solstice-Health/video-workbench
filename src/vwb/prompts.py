"""Selectable main-agent coaching, with one invariant harness contract.

Selections are presentation-safe IDs. A queued turn owns the complete prompt and
its digest, so editing a profile or changing a preference cannot change paid work
that was already submitted. Profiles never change tools, roles or policy.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from vwb.config import PROMPTS_DIR, RUNS_DIR
from vwb.util.hashing import sha256_text
from vwb.util.jsonio import read_json, write_json

DEFAULT = "descriptive"
SELECTION_NOTE = "Applies to new and resumed turns. Active turns and queued messages keep their captured prompt."
PROFILES = (
    {"id": "descriptive", "label": "Descriptive", "description": "The existing detailed Video Workbench workflow and capability guide.",
     "kind": "workbench", "file": "job_lead.md", "sources": []},
    {"id": "minimal", "label": "Minimal", "description": "A short brief with the shared harness contract and little extra coaching.",
     "kind": "workbench", "file": "main_minimal.md", "sources": []},
    {"id": "loose", "label": "Loose", "description": "Outcome-focused coaching with freedom to choose the workflow and delegation.",
     "kind": "workbench", "file": "main_loose.md", "sources": []},
    {"id": "cursor_inspired", "label": "Cursor inspired", "description": "Repository-first exploration, focused edits and concise progress, adapted from public Cursor guidance.",
     "kind": "inspired", "file": "main_cursor.md", "sources": [
         {"title": "Cursor: improving token efficiency", "url": "https://cursor.com/blog/improved-token-efficiency"},
         {"title": "Cursor agent overview", "url": "https://cursor.com/docs/agent/overview"},
         {"title": "Cursor rules", "url": "https://cursor.com/docs/rules"}]},
    {"id": "claude_code_inspired", "label": "Claude Code inspired", "description": "Explore, plan, act and verify with deliberate context and scoped delegation, adapted from public Claude Code guidance.",
     "kind": "inspired", "file": "main_claude_code.md", "sources": [
         {"title": "Claude: the new rules of context engineering", "url": "https://claude.dev/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models/"},
         {"title": "Claude Code best practices", "url": "https://code.claude.com/docs/en/best-practices"},
         {"title": "Claude Code subagents", "url": "https://code.claude.com/docs/en/sub-agents"}]},
    {"id": "codex_inspired", "label": "Codex inspired", "description": "Persistent task completion, explicit evidence and clear ownership, adapted from public Codex guidance.",
     "kind": "inspired", "file": "main_codex.md", "sources": [
         {"title": "OpenAI: rethinking skills and prompts for GPT-6 Astra", "url": "https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra"},
         {"title": "OpenAI Codex prompting guide", "url": "https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide"}]},
)


def validate(value: Any) -> str:
    if not isinstance(value, str) or value not in {row["id"] for row in PROFILES}:
        raise ValueError("Choose a known agent prompt profile.")
    return value


def profile(profile_id: str = DEFAULT) -> dict[str, Any]:
    profile_id = validate(profile_id)
    row = next(row for row in PROFILES if row["id"] == profile_id)
    content = (PROMPTS_DIR / "main_core.md").read_text().strip() + "\n\n" + (PROMPTS_DIR / row["file"]).read_text().strip()
    return {key: deepcopy(value) for key, value in row.items() if key != "file"} | {
        "content": content, "sha256": sha256_text(content)}


def _preferences_path(runs_dir: Path) -> Path:
    return runs_dir / ".ui" / "prompt_preferences.json"


def selection(ws=None, runs_dir: Path = RUNS_DIR) -> str:
    local = read_json(ws.memory / "prompt_selection.json", {}) if ws is not None else {}
    saved = local or read_json(_preferences_path(runs_dir), {}) or {}
    return validate(saved.get("profile_id", DEFAULT))


def set_selection(ws, profile_id: Any) -> str:
    chosen = validate(profile_id)
    write_json(ws.memory / "prompt_selection.json", {"profile_id": chosen})
    return chosen


def preferences(body: dict[str, Any], runs_dir: Path = RUNS_DIR) -> dict[str, Any]:
    if set(body) != {"default_profile_id"}:
        raise ValueError("Choose the default agent prompt profile.")
    write_json(_preferences_path(runs_dir), {"profile_id": validate(body["default_profile_id"])})
    return catalog(runs_dir)


def catalog(runs_dir: Path = RUNS_DIR) -> dict[str, Any]:
    return {"profiles": [profile(row["id"]) for row in PROFILES],
            "default_profile_id": selection(runs_dir=runs_dir), "selection_note": SELECTION_NOTE}


def snapshot(ws=None, runs_dir: Path = RUNS_DIR) -> dict[str, str]:
    return snapshot_profile(selection(ws, runs_dir))


def snapshot_profile(profile_id: str = DEFAULT) -> dict[str, str]:
    row = profile(profile_id)
    return {key: row[key] for key in ("id", "label", "sha256", "content")}


def validate_snapshot(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"id", "label", "sha256", "content"}:
        raise ValueError("The saved agent prompt snapshot is invalid.")
    validate(value["id"])
    if not all(isinstance(value[key], str) for key in value) or not value["content"].strip():
        raise ValueError("The saved agent prompt snapshot is invalid.")
    if sha256_text(value["content"]) != value["sha256"]:
        raise ValueError("The saved agent prompt no longer matches its captured digest.")
    return deepcopy(value)


def with_snapshot(config: dict[str, Any], ws=None, runs_dir: Path = RUNS_DIR) -> dict[str, Any]:
    result = deepcopy(config)
    result["prompt"] = validate_snapshot(result["prompt"]) if "prompt" in result else snapshot(ws, runs_dir)
    return result


def content(config: dict[str, Any] | None = None) -> str:
    # Legacy jobs without a snapshot retain the descriptive profile. A current
    # preference must not retroactively change their instructions.
    return validate_snapshot(config["prompt"])["content"] if config and "prompt" in config else profile(DEFAULT)["content"]


def status(ws, active: dict[str, Any] | None = None, runs_dir: Path = RUNS_DIR) -> dict[str, Any]:
    selected = selection(ws, runs_dir)
    captured = ((active or {}).get("main_config") or {}).get("prompt")
    frozen = validate_snapshot(captured) if captured else None
    return {"profile_id": selected, "profile": profile(selected), "active_profile_id": frozen["id"] if frozen else None,
            "active_profile": {key: frozen[key] for key in ("id", "label", "sha256")} if frozen else None,
            "selection_note": SELECTION_NOTE}
