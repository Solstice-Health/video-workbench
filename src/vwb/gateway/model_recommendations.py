"""Dated selection guidance, independent of routing, credentials and live catalogs.

These are starting points for evaluation, never an allowlist or a model alias
resolver. Refresh the evidence and LAST_UPDATED together after reviewing sources.
"""
from __future__ import annotations

import calendar
from copy import deepcopy
from datetime import UTC, date, datetime

LAST_UPDATED = date(2026, 10, 30)
REVIEW_MONTHS = 4

SOURCES = {
    "argon": "https://ai.google.dev/gemini-api/docs/models/gemini-4-argon",
    "argon_openrouter": "https://openrouter.ai/google/gemini-4-argon",
    "video_catalog": "https://openrouter.ai/api/v1/videos/models",
    "video_api": "https://openrouter.ai/docs/guides/overview/multimodal/video-generation",
    "seedance": "https://openrouter.ai/bytedance/seedance-2.5",
    "omni": "https://ai.google.dev/gemini-api/docs/models/gemini-omni-flash",
    "openai_models": "https://developers.openai.com/api/docs/models",
    "openai_selection": "https://developers.openai.com/api/docs/guides/model-selection",
    "sol_openrouter": "https://openrouter.ai/openai/gpt-6.1-sol",
    "claude_models": "https://platform.claude.com/docs/en/models/overview",
    "images": "https://developers.openai.com/api/docs/guides/image-generation",
    "google_models": "https://ai.google.dev/gemini-api/docs/models",
    "transcribe": "https://developers.openai.com/api/docs/models/gpt-transcribe",
    "embeddings": "https://developers.openai.com/api/docs/guides/embeddings",
    "multimodal_embeddings": "https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2",
}

# IDs below were checked against the cited official documentation or catalog on
# LAST_UPDATED. A documented ID is not evidence of today's account entitlement.
_TASKS = [
    {
        "task": "video_understanding",
        "guidance": "Inspect temporal events and audio with native video input; request timestamps and verify against frames.",
        "candidates": [
            {"name": "Gemini 4 Argon", "preference": "user_requested",
             "verified_ids": {"google": "gemini-4-argon", "openrouter": "google/gemini-4-argon"},
             "id_evidence": "official_documentation", "sources": ["argon", "argon_openrouter"],
             "note": "Preferred starting point for video understanding. Produces text, not video. Check the selected route's video-input support."},
        ],
    },
    {
        "task": "video_generation",
        "guidance": "Compare preferred candidates against the brief, references, motion, duration, audio, quality and cost. No universal winner.",
        "candidates": [
            {"name": "H3", "preference": "user_requested",
             "verified_ids": {"openrouter": "minimax/hailuo-3"},
             "id_evidence": "official_video_catalog", "sources": ["video_catalog", "video_api"],
             "note": "Controlled generation candidate. Discover current durations, resolution and reference support before requesting; H3 is not the slug minimax/h3."},
            {"name": "Gemini Omni 1.1", "preference": "user_requested",
             "verified_ids": {"google": "gemini-omni-1.1-flash"},
             "discovery_provider": "openrouter",
             "id_evidence": "official_documentation", "sources": ["omni", "video_catalog"],
             "note": "Generation, editing and extension candidate; prefer existing Omni tools. No OpenRouter ID found in the reviewed video catalog; do not infer one from the Google ID."},
            {"name": "Kling 4", "preference": "user_requested",
             "verified_ids": {}, "discovery_provider": "openrouter",
             "id_evidence": "unverified_candidate", "sources": ["video_catalog"],
             "note": "Requested candidate, not verified in the reviewed OpenRouter video catalog. Search current official docs and catalog; never invent a Kling 4 slug or silently substitute Kling 3."},
            {"name": "Seedance 2.5", "preference": "user_requested_family",
             "verified_ids": {"openrouter": "bytedance/seedance-2.5"},
             "id_evidence": "official_video_catalog", "sources": ["seedance", "video_catalog"],
             "note": "Candidate for reference-guided storytelling. Provider-advertised editing/extension does not imply every route exposes it; check actual input and operation support."},
        ],
    },
    {
        "task": "text_and_reasoning",
        "guidance": "Choose the lightest model that meets a representative quality check; use stronger reasoning for difficult synthesis.",
        "candidates": [
            {"name": "GPT-6.1 Sol / GPT-6 Astra",
             "verified_ids": {"openai": ["gpt-6.1-sol", "gpt-6-astra"], "openrouter": "openai/gpt-6.1-sol"},
             "id_evidence": "official_documentation", "sources": ["openai_models", "openai_selection", "sol_openrouter"],
             "note": "Sol for a quality/cost balance; Astra for demanding reasoning. Use Responses for OpenAI tool calling."},
            {"name": "Claude Opus 5.5 / Claude Fable 5.1",
             "verified_ids": {"anthropic": ["claude-opus-5-5", "claude-fable-5-1"]},
             "id_evidence": "official_documentation", "sources": ["claude_models"],
             "note": "Opus for general complex work; evaluate Fable for demanding long-horizon reasoning."},
        ],
    },
    {
        "task": "code",
        "guidance": "Evaluate changes with the repository's tests; a model's coding recommendation is not execution authority.",
        "candidates": [
            {"name": "GPT-6.1 Sol / GPT-6.1 Terra",
             "verified_ids": {"openai": ["gpt-6.1-sol", "gpt-6.1-terra"], "openrouter": "openai/gpt-6.1-sol"},
             "id_evidence": "official_documentation", "sources": ["openai_selection", "openai_models", "sol_openrouter"],
             "note": "Sol for complex code; Terra for scoped fixes and visual workflows. Use Responses for tool calling."},
            {"name": "Claude Opus 5.5",
             "verified_ids": {"anthropic": "claude-opus-5-5"},
             "id_evidence": "official_documentation", "sources": ["claude_models"],
             "note": "Candidate for sustained agentic coding and review."},
        ],
    },
    {
        "task": "image_understanding",
        "guidance": "Use original images or targeted crops; distinguish observed pixels from inferred layout or copy.",
        "candidates": [
            {"name": "Gemini 4 Argon / GPT-6.1 Terra",
             "verified_ids": {"google": "gemini-4-argon", "openai": "gpt-6.1-terra"},
             "id_evidence": "official_documentation", "sources": ["argon", "openai_selection"],
             "note": "Candidates for multimodal review and visual reasoning; preserve exact model IDs by provider."},
        ],
    },
    {
        "task": "image_generation",
        "guidance": "Prefer existing Sunburst tools for registered asset edits and masks; inspect fidelity and text after generation.",
        "candidates": [
            {"name": "GPT Image 2.5 Sunburst / Flare",
             "verified_ids": {"openai": ["gpt-image-2.5-sunburst", "gpt-image-2.5-flare"]},
             "id_evidence": "official_documentation", "sources": ["images"],
             "note": "Sunburst for demanding creation/edits; Flare for faster everyday generation."},
            {"name": "Nano Banana 2.1",
             "verified_ids": {"google": "gemini-nano-banana-2.1"},
             "id_evidence": "official_documentation", "sources": ["google_models"],
             "note": "Candidate for efficient image generation and iterative editing."},
        ],
    },
    {
        "task": "speech_generation",
        "guidance": "Use a documented voice and non-streaming operation; this gateway does not provide realtime sessions.",
        "candidates": [
            {"name": "Gemini 3.8 Flash TTS / Flash-Lite TTS",
             "verified_ids": {"google": ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]},
             "id_evidence": "official_documentation", "sources": ["google_models"],
             "note": "Flash TTS for expressive narration; evaluate Lite for latency and volume."},
        ],
    },
    {
        "task": "audio_understanding_and_transcription",
        "guidance": "Separate transcript extraction from semantic audio analysis; check supported timestamps and speaker attribution.",
        "candidates": [
            {"name": "GPT-Transcribe",
             "verified_ids": {"openai": "gpt-transcribe"},
             "id_evidence": "official_documentation", "sources": ["transcribe"],
             "note": "Candidate for completed-file transcription through audio.transcriptions."},
            {"name": "Gemini 4 Argon",
             "verified_ids": {"google": "gemini-4-argon"},
             "id_evidence": "official_documentation", "sources": ["argon"],
             "note": "Candidate for reasoning over audio content; do not infer transcription timing guarantees."},
        ],
    },
    {
        "task": "embeddings",
        "guidance": "Keep model/version, dimensions and normalization consistent with the index; changing models requires re-embedding.",
        "candidates": [
            {"name": "Gemini Embedding 2",
             "verified_ids": {"google": "gemini-embedding-2"},
             "id_evidence": "official_documentation", "sources": ["multimodal_embeddings"],
             "note": "Candidate for shared text/image/audio/video retrieval; verify native input encoding and limits."},
            {"name": "Text Embedding 3 Small / Large",
             "verified_ids": {"openai": ["text-embedding-3-small", "text-embedding-3-large"]},
             "id_evidence": "official_documentation", "sources": ["embeddings"],
             "note": "Still-documented text retrieval options: Small for cost, Large for quality. Older version numbers alone do not make a model obsolete."},
        ],
    },
]

REFRESH_INSTRUCTION = (
    "Before choosing a model, browse official provider documentation and refresh model_catalog, "
    "then verify the exact provider ID, input/output modalities, supported operation and account access. "
    "If browsing or discovery is unavailable, disclose that the guidance is stale or unverified; "
    "do not invent IDs or present these candidates as current best models. "
    "Preserve explicit user model requests; never silently rewrite, retire or substitute a model."
)


def add_calendar_months(value: date, months: int) -> date:
    """Clamp the day at month end (Oct 30 + four months is Feb 28/29)."""
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def freshness(*, today: date | None = None, last_updated: date | None = None) -> dict:
    """Compute at call time, including for servers running across the boundary."""
    current = today if today is not None else datetime.now(UTC).date()
    updated = last_updated if last_updated is not None else LAST_UPDATED
    review_after = add_calendar_months(updated, REVIEW_MONTHS)
    future_dated = current < updated
    stale = current > review_after
    return {
        "last_updated": updated.isoformat(), "as_of": current.isoformat(),
        "age_days": (current - updated).days, "review_interval_calendar_months": REVIEW_MONTHS,
        "stale_after": review_after.isoformat(), "stale": stale,
        "future_dated": future_dated, "requires_refresh": stale or future_dated,
        "refresh_instruction": REFRESH_INSTRUCTION if stale or future_dated else (
            "Check model_catalog before choosing. Once past stale_after, browse official docs "
            "and refresh the catalog before relying on this guidance."
        ),
    }


def recommendations(*, provider: str = "", today: date | None = None) -> dict:
    """Return reviewed guidance separately from live catalog rows/configuration."""
    tasks = deepcopy(_TASKS)
    if provider:
        for task in tasks:
            task["candidates"] = [c for c in task["candidates"]
                                  if provider in c["verified_ids"] or c.get("discovery_provider") == provider]
        tasks = [task for task in tasks if task["candidates"]]
    source_names = {s for task in tasks for c in task["candidates"] for s in c["sources"]}
    return {
        **freshness(today=today),
        "selection_policy": (
            "Task-specific starting points, not a universal ranking, routing default or account-access guarantee. "
            "verified_ids records evidence on last_updated; live catalog items remain authoritative for discovery. "
            "Do not copy historical examples as defaults or silently change an explicit request. "
            "IDs for one provider do not imply the corresponding OpenRouter route exists."
        ),
        "tasks": tasks, "sources": {s: url for s, url in SOURCES.items() if s in source_names},
    }


REQUEST_GUIDANCE = (
    f" Selection guidance last_updated={LAST_UPDATED.isoformat()} (review after four calendar months): "
    "video understanding: Gemini 4 Argon; video generation: H3 (OpenRouter minimax/hailuo-3), "
    "Gemini Omni 1.1 (existing Google Omni tools), Seedance 2.5; Kling 4 is a preferred candidate "
    "without a verified OpenRouter ID. Text/code: GPT-6.1 Sol or Claude Opus 5.5; "
    "images: Sunburst/Flare or Nano Banana 2.1; speech: Gemini 3.8 TTS; transcription: GPT-Transcribe; "
    "embeddings: Gemini Embedding 2 or text-embedding-3 Small/Large. "
    "Get runtime age, evidence and refresh instructions from model_providers/model_catalog before choosing. "
    "When older than four calendar months, browse official docs and refresh discovery. "
    "These are candidates, not universal winners; never silently replace an explicit model request."
)
