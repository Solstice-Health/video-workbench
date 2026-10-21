"""Durable image replay, native-host adapters, recovery and compaction without paid calls."""
from __future__ import annotations

import base64
import copy
import io
import json

import pytest
from PIL import Image

from vwb.agents import attachment_inputs as inputs
from vwb.agents import host_codex, job_lead, provider_loop
from vwb.gateway import Gateway
from vwb.tools import build_registry
from vwb.tools.base import Registry, ToolContext
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import Workspace, attachments
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "r-attachment-inputs")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Inspect these files"))
    return ToolContext(ws, "job_lead", "job_lead", Gateway(ws, actor="job_lead"))


def image_ref(ctx, tmp_path):
    path = tmp_path / "reference.png"
    Image.new("RGB", (20, 10), "red").save(path)
    ref, = attachments.ingest(ctx.ws, [path])
    ctx.ws.add_chat("human", "What does this show?", attachments=[ref])
    return ref


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_provider_rehydrates_original_pixels_without_persisting_base64(ctx, tmp_path, monkeypatch, provider):
    ref = image_ref(ctx, tmp_path)
    Image.new("RGB", (20, 10), "blue").save(ctx.ws.work / ref["path"])
    calls = []
    response = ({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Red."}]}
                if provider == "anthropic" else {"choices": [{"message": {"content": "Red."}, "finish_reason": "stop"}]})
    def post(_provider, body, _activity):
        calls.append(copy.deepcopy(body))
        return response
    monkeypatch.setattr(provider_loop, "_post", post)
    cfg = {"provider": provider, "host": f"{provider}_api", "model": "test", "max_turns": 2,
           "image_input_supported": True}
    provider_loop.run(ctx, Registry([]), cfg, "System", "Inspect", turn_id="1", attachment_refs=[ref])
    path = provider_loop.history_path(ctx, provider, "test")
    saved = path.read_text()
    assert "base64" not in saved and inputs.IMAGE_REF in saved
    user = next(m for m in calls[0]["messages"] if m["role"] == "user")
    block = user["content"][1]
    encoded = block["source"]["data"] if provider == "anthropic" else block["image_url"]["url"].split(",")[1]
    rgb = Image.open(io.BytesIO(base64.b64decode(encoded))).getpixel((0, 0))
    assert rgb[0] > 240 and rgb[2] < 10
    registry = build_registry()
    remembered = registry.call(ctx, "attachment_remember", {"id": ref["id"], "summary": "A red reference swatch.",
                                                           "image_mode": "reference"})
    assert not remembered.is_error
    provider_loop.run(ctx, Registry([]), cfg, "System", "Continue", turn_id="2")
    assert "base64" not in json.dumps(calls[-1])
    assert "A red reference swatch" in json.dumps(calls[-1])
    assert inputs.IMAGE_REF in path.read_text()  # changing replay strategy does not erase history
    reopened = registry.call(ctx, "attachment_read", {"id": ref["id"]})
    assert reopened.images and not reopened.is_error


def test_responses_and_codex_inputs_are_native_and_mount_only_payloads_readonly(ctx, tmp_path):
    ref = image_ref(ctx, tmp_path)
    parts = inputs.input_parts(ctx.ws, "Look", [ref], "openai")
    assert parts[0] == {"type": "input_text", "text": "Look"}
    assert parts[1]["type"] == "input_image" and parts[1]["image_url"].startswith("data:image/jpeg;base64,")
    mounts, args = host_codex._attachment_images(ctx.ws, [ref])
    assert mounts[0] == "-v" and mounts[1].endswith(":ro")
    assert "/.attachments/" in mounts[1] and "original" not in mounts[1]
    assert args == ["--image", f"/vwb-attachments/{ref['id']}/payload.jpg"]


def test_hydration_only_interprets_user_owned_markers_and_prefers_recent_images(ctx, tmp_path, monkeypatch):
    first = image_ref(ctx, tmp_path)
    second = image_ref(ctx, tmp_path)
    signed = {"role": "assistant", "content": [{"type": "tool_use", "input": inputs.content("x", [first])}]}
    native = {"role": "tool", "content": "untouched", "reasoning_details": [{"type": "signed", "data": "opaque"}]}
    messages = [{"role": "user", "content": inputs.content("Earlier", [first])}, signed,
                {"role": "user", "content": inputs.content("Latest", [second])}, native]
    before = copy.deepcopy(messages)
    monkeypatch.setattr(inputs, "MAX_INLINE_IMAGES", 1)
    wire = inputs.hydrate(ctx.ws, messages, "anthropic")
    assert wire[0]["content"][1]["type"] == "text"
    assert "payload budget" in wire[0]["content"][1]["text"]
    assert wire[2]["content"][1]["type"] == "image"
    assert wire[1] == signed and wire[3] == native and messages == before


def test_missing_image_is_explicit_and_keeps_conversation_replayable(ctx, tmp_path):
    ref = image_ref(ctx, tmp_path)
    attachments.original_path(ctx.ws, ref).unlink()
    parts = inputs.input_parts(ctx.ws, "Continue", [ref], "anthropic")
    assert parts[-1]["type"] == "text" and "unavailable" in parts[-1]["text"]


def test_text_only_model_keeps_attachment_reference_without_decoding_pixels(ctx, tmp_path, monkeypatch):
    ref = image_ref(ctx, tmp_path)
    monkeypatch.setattr(attachments, "image_payload", lambda *_: pytest.fail("Do not decode images for a text-only model"))
    raw = inputs.content("Inspect", [ref])
    wire = inputs.hydrate(ctx.ws, raw, "openrouter", allow_images=False)
    assert wire[1]["type"] == "text"
    assert ref["id"] in wire[1]["text"] and ref["path"] in wire[1]["text"]
    assert "no pixels were sent" in wire[1]["text"]
    assert raw[1]["type"] == inputs.IMAGE_REF


def test_compaction_preserves_originals_refs_and_saved_observations(ctx, tmp_path, monkeypatch):
    ref = image_ref(ctx, tmp_path)
    registry = build_registry()
    registry.call(ctx, "attachment_remember", {"id": ref["id"], "summary": "A red swatch."})
    before = ctx.ws.request_path.read_bytes()
    write_json(ctx.ws.memory / "lead_session.json", {"key": "codex:test", "threads": {
        "codex:test": {"thread_id": "t1", "config": {"model": "test"}}}})
    monkeypatch.setattr(host_codex, "compact_thread", lambda *_: None)
    job_lead.compact(ctx.ws)
    assert ctx.ws.request_path.read_bytes() == before
    assert read_json(ctx.ws.memory / "attachments.json")["files"] == [ref]
    listing = registry.call(ctx, "attachment_list", {}).data
    assert listing["files"][0]["memory"]["summary"] == "A red swatch."
    assert attachments.original_path(ctx.ws, ref).is_file()


def test_native_host_switch_receives_missed_attachment_pixels_and_paths(ctx, tmp_path, monkeypatch):
    ref = image_ref(ctx, tmp_path)
    ctx.ws.add_chat("job_lead", "Red image.")
    ctx.ws.add_chat("human", "Continue")
    captured = []
    monkeypatch.setattr(job_lead, "surface", lambda _: {"tools": {}, "docs": {}})
    def run(*args, **kwargs):
        captured.append((args, kwargs))
        kwargs["session"]["thread_id"] = "t1"
        return "Done"
    monkeypatch.setattr(host_codex, "run_lead", run)
    state = {}
    job_lead._run_host(ctx.ws, Registry([]), None, "Continue", False, {"host": "codex", "model": "test"},
                       ctx.ledger, state, [])
    assert captured[0][1]["attachment_refs"] == [ref]
    assert ref["path"] in captured[0][0][3]


@pytest.mark.parametrize(("name", "args"), [
    ("attachment_list", {"limit": 0}), ("attachment_list", {"limit": 101}),
    ("attachment_list", {"offset": -1}), ("attachment_list", {"offset": True}),
    ("attachment_remember", {"summary": "x" * 6001}),
    ("attachment_remember", {"summary": "x", "image_mode": "delete"}),
])
def test_runtime_bounds_are_enforced_not_just_declared(ctx, tmp_path, name, args):
    ref = image_ref(ctx, tmp_path)
    assert build_registry().call(ctx, name, {"id": ref["id"], **args}).is_error


def test_text_and_unknown_binary_are_not_misrepresented(ctx, tmp_path):
    text = tmp_path / "notes.md"
    text.write_text("abc" * 20_000)
    binary = tmp_path / "mystery.dat"
    binary.write_bytes(b"\x00\xff\x01")
    refs = attachments.ingest(ctx.ws, [text, binary])
    registry = build_registry()
    part = registry.call(ctx, "attachment_read", {"id": refs[0]["id"]}).data
    assert len(part["text"]) == 32768 and part["next_offset_bytes"] == 32768
    unknown = registry.call(ctx, "attachment_read", {"id": refs[1]["id"]}).data
    assert not unknown["content_read"] and "appropriate parser" in unknown["next_step"]
