"""Validated main-agent selections and presentation-safe provider/model settings."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from filelock import FileLock

from vwb.config import RUNS_DIR, load_config, provider_status, slot
from vwb.util.jsonio import read_json, write_json

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT = {"model_id": "astra", "context_tokens": 1000000, "effort": "max",
           "fast": False, "ultra_fast": False, "custom_model": None}


def _path(runs_dir: Path) -> Path:
    return runs_dir / ".ui" / "model_preferences.json"


def _preferences(runs_dir: Path) -> dict:
    return read_json(_path(runs_dir), {}) or {}


def _descriptor(row: dict, statuses: dict) -> dict:
    ready = statuses.get(row["provider"], False)
    limit = row.get("native_context", 0)
    return {**row, "available": ready and row.get("tools_supported", True),
            "unavailable_reason": "" if ready else f"Add your {row['provider']} API key in Provider APIs.",
            "capabilities": {"contexts": [n for n in [300000, 1000000] if limit >= n] or ([limit] if limit else []),
                             "efforts": row.get("efforts", []), "fast": row.get("fast", False),
                             "ultra_fast": row.get("ultra_fast", False)}}


def catalog(runs_dir: Path = RUNS_DIR) -> dict:
    prefs = _preferences(runs_dir)
    providers = provider_status()
    states = {p["id"]: p["configured"] for p in providers}
    enabled = prefs.get("enabled_model_ids", load_config("models")["default_enabled"])
    rows = [_descriptor(row, states) for row in load_config("models")["models"]]
    rows.append(_descriptor({"id": "custom_openrouter", "label": "Custom OpenRouter", "provider": "openrouter",
                            "model": "", "host": "openrouter_api", "native_context": 0,
                            "efforts": [], "fast": False, "ultra_fast": False}, states))
    for row in rows:
        row["enabled"] = row["id"] in enabled
    selected = prefs.get("default_selection", DEFAULT)
    return {"models": rows, "providers": providers, "enabled_model_ids": enabled,
            "default_selection": selected, "selected_model": describe(selected, runs_dir, statuses=states),
            "context_note": "Working-context budget. Model-native limits are separate; larger requests can cost more."}


def canonical_openrouter(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Enter an exact OpenRouter model ID, such as openrouter/meta/muse-wonder.")
    value = value.strip().removeprefix("openrouter/")
    if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.:+-]+", value):
        raise ValueError("Use the exact provider/model ID from OpenRouter, prefixed with openrouter/ if you prefer.")
    return value


def resolve(value: Any, runs_dir: Path = RUNS_DIR) -> dict:
    model_id = canonical_openrouter(value)
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get("https://openrouter.ai/api/v1/models")
            if response.status_code != 200:
                raise ValueError(f"OpenRouter catalog unavailable (HTTP {response.status_code}). Try again.")
            data = response.json()
            source = next((m for m in data.get("data", []) if m.get("id") == model_id), None)
            if source is None:
                raise ValueError("That exact model ID is not in OpenRouter's current catalog.")
            params = source.get("supported_parameters", [])
            if "tools" not in params:
                raise ValueError("This model does not support the tool calls the main agent needs.")
            limit = source.get("context_length", 0)
            if not isinstance(limit, int) or limit <= 0:
                raise ValueError("This model does not report a usable context limit.")
            reasoning = source.get("reasoning") or {}
            reported = reasoning.get("supported_efforts", [])
            efforts = [e for e in EFFORTS if e in reported]
            # Without explicit capability metadata, don't invent five compatible settings.
            row = {"id": "custom_openrouter", "label": source.get("name", model_id), "provider": "openrouter",
                   "model": model_id, "canonical_id": model_id, "host": "openrouter_api", "native_context": limit,
                   "efforts": efforts, "fast": False, "ultra_fast": False, "tools_supported": True,
                   "max_output_tokens": min((source.get("top_provider") or {}).get("max_completion_tokens") or 16384, 16384)}
            # Tier availability is endpoint-specific; query the endpoint tags before showing speed controls.
            endpoint = client.get(f"https://openrouter.ai/api/v1/models/{model_id}/endpoints")
            if endpoint.status_code == 200:
                endpoints = endpoint.json().get("data", {}).get("endpoints", [])
                names = " ".join(str(e.get("tag", "")) for e in endpoints).lower()
                row["fast"] = bool(re.search(r"(?:/|\b)fast\b|priority", names))
                row["ultra_fast"] = "ultrafast" in names
    except (httpx.HTTPError, json.JSONDecodeError):
        raise ValueError("Could not read OpenRouter's model catalog. Check your connection and try again.") from None
    write_json(runs_dir / ".ui" / "resolved_models" / f"{_model_hash(model_id)}.json", row)
    states = {p["id"]: p["configured"] for p in provider_status()}
    return _descriptor(row, states)


def _model_hash(model: str) -> str:
    from vwb.util.hashing import sha256_text
    return sha256_text(model)[:24]


def describe(selected: dict, runs_dir: Path = RUNS_DIR, *, statuses: dict | None = None) -> dict | None:
    statuses = statuses if statuses is not None else {p["id"]: p["configured"] for p in provider_status()}
    if selected.get("model_id") == "custom_openrouter":
        name = selected.get("custom_model")
        if not isinstance(name, str):
            return None
        row = read_json(runs_dir / ".ui" / "resolved_models" / f"{_model_hash(name)}.json")
    else:
        row = next((m for m in load_config("models")["models"] if m["id"] == selected.get("model_id")), None)
    return _descriptor(row, statuses) if row else None


def validate(value: Any, runs_dir: Path = RUNS_DIR, *, require_key: bool = True) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError("Choose a model and its supported context, effort and speed settings.")
    selected = {**DEFAULT, **value}
    rows = catalog(runs_dir)["models"]
    model = next((m for m in rows if m["id"] == selected["model_id"]), None)
    if model is None:
        raise ValueError("Unknown main-agent model.")
    if model["id"] == "custom_openrouter":
        custom = canonical_openrouter(selected.get("custom_model"))
        source = read_json(runs_dir / ".ui" / "resolved_models" / f"{_model_hash(custom)}.json")
        if not source:
            raise ValueError("Check this exact OpenRouter model before selecting it.")
        model = _descriptor(source, {p["id"]: p["configured"] for p in provider_status()})
        selected["custom_model"] = custom
    else:
        selected["custom_model"] = None
    if require_key and not model["available"]:
        raise ValueError(model["unavailable_reason"])
    cap = model["capabilities"]
    if type(selected["context_tokens"]) is not int or selected["context_tokens"] not in cap["contexts"]:
        raise ValueError("Choose a context budget supported by this model.")
    if cap["efforts"]:
        if selected["effort"] not in cap["efforts"]:
            raise ValueError("Choose a reasoning effort supported by this model.")
    elif selected["effort"] is not None:
        raise ValueError("This model does not report configurable reasoning effort; use its default.")
    for field in ("fast", "ultra_fast"):
        if type(selected[field]) is not bool or (selected[field] and not cap[field]):
            raise ValueError(f"{field.replace('_', ' ').title()} is unavailable for this model.")
    if selected["fast"] and selected["ultra_fast"]:
        raise ValueError("Choose either Fast or Ultrafast.")
    return selected


def selection(ws=None, runs_dir: Path = RUNS_DIR) -> dict:
    if ws is not None:
        saved = read_json(ws.memory / "model_selection.json")
        if saved:
            return saved
    return _preferences(runs_dir).get("default_selection", dict(DEFAULT))


def explicit_runtime(ws=None, runs_dir: Path = RUNS_DIR) -> dict | None:
    """Old conversations retain their configured slot/fallbacks until a UI choice is saved."""
    selected = read_json(ws.memory / "model_selection.json") if ws is not None else None
    selected = selected or _preferences(runs_dir).get("default_selection")
    return runtime(selected, runs_dir) if selected else None


def set_selection(ws, value: Any, runs_dir: Path = RUNS_DIR) -> dict:
    selected = validate(value, runs_dir)
    write_json(ws.memory / "model_selection.json", selected)
    return selected


def preferences(body: dict, runs_dir: Path = RUNS_DIR) -> dict:
    path = _path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        prefs = _preferences(runs_dir)
        if "enabled_model_ids" in body:
            enabled = body["enabled_model_ids"]
            known = {m["id"] for m in catalog(runs_dir)["models"]}
            if not isinstance(enabled, list) or not enabled or any(not isinstance(m, str) or m not in known for m in enabled):
                raise ValueError("Keep at least one known model in the dropdown.")
            prefs["enabled_model_ids"] = list(dict.fromkeys(enabled))
        if "default_selection" in body:
            prefs["default_selection"] = validate(body["default_selection"], runs_dir)
        write_json(path, prefs)
    return catalog(runs_dir)


def runtime(selected: dict, runs_dir: Path = RUNS_DIR) -> dict:
    selected = validate(selected, runs_dir)
    if selected["model_id"] == "custom_openrouter":
        row = read_json(runs_dir / ".ui" / "resolved_models" / f"{_model_hash(selected['custom_model'])}.json")
    else:
        row = next(m for m in load_config("models")["models"] if m["id"] == selected["model_id"])
    budget = selected["context_tokens"]
    return {"model": row["model"], "provider": row["provider"], "host": row["host"],
            "effort": selected["effort"], "context_window": budget, "native_context_window": row["native_context"],
            "auto_compact_tokens": int(budget * 0.85), "max_turns": int(slot("main").get("max_turns", 400)),
            "max_output_tokens": row.get("max_output_tokens", 16384), "fallbacks": [],
            "service_tier": "ultrafast" if selected["ultra_fast"] else "fast" if selected["fast"] else "default",
            "speed": "fast" if selected["fast"] else "standard", "selection": selected}
