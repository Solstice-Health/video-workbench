"""Paths, secrets and YAML configuration.

Secrets are read from a .env file into a `Secrets` object that only the gateway uses.
We never copy the whole .env into os.environ, so agent subprocesses do not inherit
unrelated keys (spec invariant I3: vendor keys stay out of the agent plane).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from filelock import FileLock

PACKAGE_DIR = Path(__file__).resolve().parent
_SOURCE_ROOT = PACKAGE_DIR.parents[1]
SOURCE_CHECKOUT = ((_SOURCE_ROOT / "pyproject.toml").is_file()
                   and (_SOURCE_ROOT / "config" / "slots.yaml").is_file()
                   and (_SOURCE_ROOT / "src" / "vwb" / "config.py").resolve() == Path(__file__).resolve())
# Editable checkouts keep their existing paths. Installed wheels carry immutable
# resources beside the package, while state and .env discovery belong to the
# directory the user launched from, never a Python installation's lib/ directory.
REPO_ROOT = _SOURCE_ROOT if SOURCE_CHECKOUT else Path.cwd().resolve()
RESOURCE_ROOT = _SOURCE_ROOT if SOURCE_CHECKOUT else PACKAGE_DIR / "_resources"
CONFIG_DIR = RESOURCE_ROOT / "config"
RECIPES_DIR = RESOURCE_ROOT / "recipes"
DOCS_DIR = RESOURCE_ROOT / "docs"
PROMPTS_DIR = PACKAGE_DIR / "agents" / "prompts"
GENERATORS_DIR = RESOURCE_ROOT / "generators"
RUNS_DIR = Path(os.environ.get("VWB_RUNS_DIR", REPO_ROOT / "runs"))

_SECRET_KEYS = ("OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY", "OPENROUTER_API_KEY")
PROVIDERS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
             "google": "GOOGLE_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
_PROVIDER_ALIASES = {"google": ("GEMINI_API_KEY",)}


def _provider_baseline(provider: str, file_values: dict[str, Any]) -> tuple[str | None, str]:
    """Resolve aliases once, with process settings before the environment file."""
    keys = (PROVIDERS[provider], *_PROVIDER_ALIASES.get(provider, ()))
    for source, values in (("environment", os.environ), (".env", file_values)):
        for key in keys:
            if values.get(key):
                return values[key], source
    return None, "Not configured"


def _managed_keys() -> dict[str, str]:
    import json
    path = RUNS_DIR / ".keys" / "provider_keys.json"
    if not path.exists():
        return {}
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("The provider key store must be a regular local file.")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(k not in PROVIDERS or not isinstance(v, str) for k, v in data.items()):
        raise RuntimeError("The provider key store has an invalid format.")
    return data


def provider_status() -> list[dict[str, Any]]:
    managed = _managed_keys()
    env_file = find_env_file()
    values = dotenv_values(env_file) if env_file else {}
    labels = {"openai": "OpenAI", "anthropic": "Anthropic", "google": "Google", "openrouter": "OpenRouter"}
    rows = []
    for provider in PROVIDERS:
        baseline, source = _provider_baseline(provider, values)
        if provider in managed:
            source = "Workbench" if managed[provider] else "Disabled"
        rows.append({"id": provider, "label": labels[provider], "configured": bool(managed.get(provider, baseline)),
                     "source": source, "can_reset": provider in managed and bool(baseline)})
    return rows


def update_provider(provider: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Host-only overrides; an empty value disconnects without editing the user's .env."""
    import json
    import tempfile
    if provider not in PROVIDERS or body.get("action") not in {"save", "clear", "reset"}:
        raise ValueError("Choose a known provider and Save, Disconnect or Use .env key.")
    action, key = body["action"], body.get("api_key")
    if action == "save" and (not isinstance(key, str) or not 8 <= len(key) <= 4096 or any(c.isspace() for c in key)):
        raise ValueError("Enter a valid API key without spaces.")
    directory = RUNS_DIR / ".keys"
    if directory.is_symlink():
        raise ValueError("The provider key store must be a regular local folder.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "provider_keys.json"
    with FileLock(str(path) + ".lock"):
        values = _managed_keys()
        if action == "reset":
            values.pop(provider, None)
        else:
            values[provider] = key if action == "save" else ""
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".provider-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(values, handle)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            Path(temporary).unlink(missing_ok=True)
    secrets.cache_clear()
    from vwb.gateway.core import Gateway
    with Gateway._client_lock:
        Gateway._clients.clear()
    return provider_status()


def find_env_file() -> Path | None:
    """VWB_ENV_FILE wins; otherwise walk up from the checkout or launch directory."""
    explicit = os.environ.get("VWB_ENV_FILE")
    if explicit:
        return Path(explicit)
    for parent in [REPO_ROOT, *REPO_ROOT.parents]:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class Secrets:
    openai: str | None
    google: str | None
    anthropic: str | None
    exa: str | None
    openrouter: str | None = None

    def require(self, name: str) -> str:
        value = getattr(self, name)
        if not value:
            raise RuntimeError(f"Missing {name} key. Add it to the .env file (see .env.example).")
        return value


@lru_cache(maxsize=1)
def secrets() -> Secrets:
    values: dict[str, str | None] = {k: os.environ.get(k) for k in _SECRET_KEYS}
    env_file = find_env_file()
    if env_file:
        file_values = dotenv_values(env_file)
        for key in _SECRET_KEYS:
            values[key] = values[key] or file_values.get(key)
    else:
        file_values = {}
    for provider, key in PROVIDERS.items():
        values[key] = _provider_baseline(provider, file_values)[0]
    for provider, value in _managed_keys().items():
        values[PROVIDERS[provider]] = value
    return Secrets(
        openai=values["OPENAI_API_KEY"],
        google=values["GOOGLE_API_KEY"],
        anthropic=values["ANTHROPIC_API_KEY"],
        exa=values["EXA_API_KEY"],
        openrouter=values["OPENROUTER_API_KEY"],
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


@cache
def load_config(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml (slots, policy, levers, flag_kinds)."""
    return _load_yaml(CONFIG_DIR / f"{name}.yaml")


@cache
def load_profile(profile_id: str) -> dict[str, Any]:
    path = CONFIG_DIR / "profiles" / f"{profile_id}.yaml"
    if not path.is_file():
        known = sorted(p.stem for p in (CONFIG_DIR / "profiles").glob("*.yaml"))
        raise KeyError(f"Unknown platform profile {profile_id!r}. Known: {known}")
    return _load_yaml(path)


def slot(name: str) -> dict[str, Any]:
    slots = load_config("slots")
    if name not in slots:
        raise KeyError(f"Unknown model slot {name!r}")
    return dict(slots[name])


def lever(name: str) -> dict[str, Any]:
    return dict(load_config("levers").get("levers", {}).get(name, {"mode": "serve"}))


def lever_serves(name: str) -> bool:
    return lever(name).get("mode", "serve") == "serve"
