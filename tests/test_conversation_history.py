"""Message-level history, independent files and fresh provider replay; no paid API."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vwb.agents import attachment_inputs, job_lead, provider_loop
from vwb.tools.base import Registry
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import attachments, checkpoints, conversations
from vwb.workspace.checkpoints import HistoryConflict
from vwb.workspace.ledger import Ledger
from vwb.workspace.projects import ProjectStore
from vwb.workspace.registry import Registry as AssetRegistry
from vwb.workspace.request import SUBDIRS, ChatTurn, JobSpec, Request, Workspace


@pytest.fixture
def ws(tmp_path):
    source = Workspace(tmp_path / "runs" / "r-original")
    for name in SUBDIRS:
        (source.root / name).mkdir(parents=True, exist_ok=True)
    source.save(Request(id=source.id, message="Original brief", chat=[
        ChatTurn(role="human", text="Original brief"), ChatTurn(role="job_lead", text="First response"),
        ChatTurn(role="human", text="Later instruction"), ChatTurn(role="job_lead", text="Future response"),
    ]))
    return source


def payload(ws, action="rewind", index=1, **extra):
    return {"action": action, "message_id": ws.load().chat[index].id,
            "expected_revision": ws.load().history_revision, "operation_id": f"operation-{action}", **extra}


def attach(ws, tmp_path, index=0, *, name="original.txt", content=b"Pinned original"):
    file = tmp_path / name
    file.write_bytes(content)
    ref, = attachments.ingest(ws, [file])
    req = ws.load()
    req.chat[index].attachments = [ref]
    req.chat[index].context = {"files": [ref["path"]], "plugins": ["figma"]}
    ws.save(req)
    return ref


def future_state(ws):
    write_json(ws.memory / "lead_session.json", {"seq": 0, "key": "codex:test", "threads": {
        "codex:test": {"thread_id": "future-thread"}, "claude_sdk:test": {"thread_id": "future-claude"}}})
    write_json(ws.memory / "providers" / "future.json", {"messages": ["FUTURE_NATIVE"]})
    write_json(ws.memory / "plan.json", {"steps": ["FUTURE_PLAN"]})
    (ws.memory / "notes.md").write_text("FUTURE_NOTES")
    write_json(ws.memory / "attachment_notes.json", {"summary": "FUTURE_SUMMARY"})
    write_json(ws.memory / "active_host.json", {"thread_id": "future-thread"})
    write_json(ws.memory / "provider_context.json", {"used_tokens": 100})
    write_json(ws.memory / "chat_events.jsonl", {"kind": "message", "text": "FUTURE_EVENTS"})
    write_json(ws.root / "agent" / "codex" / "sessions" / "future.json", {"thread_id": "future-thread"})
    write_json(ws.asks / "tickets" / "future.json", {
        "id": "future", "status": "answered", "question": "FUTURE_QUESTION", "answer": "FUTURE_ANSWER"})
    write_json(ws.root / ".code" / "editor_context.json", {"path": "future.txt"})
    req = ws.load()
    req.spec = JobSpec(summary="FUTURE_SPEC")
    req.outputs = ["out"]
    ws.save(req)


@pytest.mark.parametrize("index", [0, 1, 3, None])
def test_fork_inclusive_boundary_or_latest_preserves_ids_and_current_files(ws, index):
    req = ws.load()
    (ws.work / "current.txt").write_text("Current file, not a historical snapshot")
    future_state(ws)
    target = ws.root.parent / "r-fork"
    out = conversations.apply(ws, target, payload(ws, "fork", message_id=req.chat[index].id if index is not None else None))
    fork = out["workspace"]
    expected = req.chat[:index + 1] if index is not None else req.chat
    assert fork.load().chat == expected
    assert ws.load().chat == req.chat
    assert fork.load().id == target.name and fork.load().status == "open"
    assert (fork.work / "current.txt").read_text().startswith("Current file")
    (fork.work / "current.txt").write_text("Changed independently")
    assert (ws.work / "current.txt").read_text().startswith("Current file")
    assert fork.load().spec is None and fork.load().outputs == ["out"]
    assert not (fork.memory / "providers").exists() and not (fork.root / "agent").exists()
    assert read_json(fork.memory / "lead_session.json")["threads"] == {}
    assert out["result"]["filesystem"] == "current"


def test_fork_pins_only_retained_attachment_originals_and_copies_source_bytes(ws, tmp_path):
    first = attach(ws, tmp_path)
    future = attach(ws, tmp_path, index=2, name="later.txt", content=b"Later attachment")
    (ws.work / first["path"]).write_bytes(b"Edited working copy")
    media = tmp_path / "source.mp4"
    media.write_bytes(b"Actual source video bytes")
    req = ws.load()
    req.sources = [ws.add_source(media, "src1")]
    ws.save(req)
    fork = conversations.fork_conversation(ws, ws.root.parent / "r-fork", req.chat[1].id)
    assert attachments.original_path(fork, first).read_bytes() == b"Pinned original"
    assert (fork.work / first["path"]).read_bytes() == b"Edited working copy"
    assert not (fork.root / ".attachments" / future["id"]).exists()
    assert attachments.original_path(ws, first).stat().st_ino != attachments.original_path(fork, first).stat().st_ino
    copied_source = Path(fork.load().sources[0].path)
    assert copied_source.read_bytes() == media.read_bytes()
    assert copied_source.is_relative_to(fork.media)
    assert copied_source.stat().st_ino != Path(req.sources[0].path).stat().st_ino
    assert fork.load().chat[0].context == req.chat[0].context


def test_fork_preserves_live_assets_without_source_paths_or_private_stores(ws, tmp_path):
    asset = tmp_path / "asset.txt"
    asset.write_text("pixels")
    ref = AssetRegistry(ws.assets).register(asset, slot="test", kind="image", receipt="human.upload", derived_from=[])
    (ws.canvas / "out").mkdir()
    (ws.canvas / "out" / "HEAD").write_text("1")
    write_json(ws.canvas / "out" / "v1.json", {"asset": ref})
    video = ws.renders / "out" / "v1" / "out_v1.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"Current rendered bytes")
    write_json(video.parent / "receipt.json", {"file": str(video), "frames": 6})
    req = ws.load()
    req.outputs = ["out"]
    ws.save(req)
    for relative in (".keys/key", ".browser/cookies", ".qc-chat/items.json", "memory/web_jobs/paid.json",
                     "agent/codex/auth.json", "work/.env", "work/.sandbox/envs/huge", "work/.qc-chat/private"):
        path = ws.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE")
    fork = conversations.fork_conversation(ws, ws.root.parent / "r-fork")
    row = AssetRegistry(fork.assets).get(ref)
    assert Path(row["path"]).is_relative_to(fork.assets)
    assert Path(row["path"]).read_text() == "pixels"
    assert (fork.canvas / "out" / "HEAD").read_text() == "1"
    receipt = read_json(fork.renders / "out" / "v1" / "receipt.json")
    assert Path(receipt["file"]).is_relative_to(fork.renders)
    assert Path(receipt["file"]).read_bytes() == b"Current rendered bytes"
    for relative in (".keys", ".browser", ".qc-chat", "memory/web_jobs", "agent", "work/.env", "work/.sandbox", "work/.qc-chat"):
        assert not (fork.root / relative).exists()


def test_project_fork_shares_work_but_not_attachments_or_assets(ws, tmp_path):
    ref = attach(ws, tmp_path)
    projects = ProjectStore(ws.root.parent)
    project = projects.create("Editing project")
    projects.move(ws, project["id"])
    (ws.work / "shared.txt").write_text("Shared work")
    target = ws.root.parent / "r-project-fork"
    fork = conversations.fork_conversation(ws, target, ws.load().chat[1].id)
    assert fork.load().project_id == project["id"]
    assert fork.work == ws.work and not fork.work.is_symlink()
    assert list((fork.root / "work").iterdir()) == []
    assert fork.assets != ws.assets
    assert attachments.original_path(fork, ref).read_bytes() == b"Pinned original"
    assert attachments.original_path(fork, ref).stat().st_ino != attachments.original_path(ws, ref).stat().st_ino


def test_rewind_quarantines_future_context_preserves_outputs_and_ledger(ws):
    future_state(ws)
    original = ws.load()
    ledger = Ledger(ws.ledger / "ledger.jsonl")
    old_event = ledger.append("human.answer", "human", {"ticket": "later", "text": "FUTURE_EVENT"})
    signed_history = (ws.ledger / "ledger.jsonl").read_bytes()
    write_json(ws.memory / "model_selection.json", {"model": "kept-setting"})
    write_json(ws.root / ".qc-chat" / "items.json", {"private": "current QC"})
    (ws.work / "current.txt").write_text("Current files")
    out = conversations.apply(ws, None, payload(ws))
    backup = Path(out["result"]["backup_path"])
    assert ws.load().chat == original.chat[:2]
    assert ws.load().history_revision == original.history_revision + 1
    assert ws.load().outputs == original.outputs
    assert (ws.work / "current.txt").read_text() == "Current files"
    assert read_json(backup / "request-before.json")["chat"] == original.model_dump(mode="json")["chat"]
    assert (backup / "saved" / "memory" / "notes.md").read_text() == "FUTURE_NOTES"
    assert (backup / "saved" / "agent" / "codex" / "sessions" / "future.json").exists()
    for relative in ("memory/providers", "memory/notes.md", "memory/plan.json", "memory/attachment_notes.json",
                     "memory/chat_events.jsonl", "memory/active_host.json", "agent", ".code/editor_context.json"):
        assert not (ws.root / relative).exists()
    assert not list((ws.asks / "tickets").glob("*.json"))
    assert read_json(ws.memory / "model_selection.json")["model"] == "kept-setting"
    assert read_json(ws.root / ".qc-chat" / "items.json")["private"] == "current QC"
    assert (ws.ledger / "ledger.jsonl").read_bytes() == signed_history
    assert checkpoints.read_checkpoint(ws)["ledger_seq"] == old_event["seq"]
    assert "FUTURE" not in job_lead.wake_context(ws)


@pytest.mark.parametrize("fork", [False, True])
def test_edit_truncates_before_human_and_returns_exact_references_for_enqueue(ws, tmp_path, fork):
    ref = attach(ws, tmp_path, index=2)
    req = ws.load()
    target = ws.root.parent / "r-edited" if fork else None
    out = conversations.edit_conversation(ws, req.chat[2].id, "Edited instruction", target=target, operation_id="edit")
    edited = out["result"]["edited_message"]
    assert out["workspace"].load().chat == req.chat[:2]
    assert edited == {"text": "Edited instruction", "context": req.chat[2].context,
                      "attachments": [ref], "replaces_message_id": req.chat[2].id}
    assert attachments.original_path(out["workspace"], ref).read_bytes() == b"Pinned original"
    if fork:
        assert ws.load().chat == req.chat
    else:
        assert read_json(Path(out["result"]["backup_path"]) / "request-before.json")["chat"][2]["text"] == req.chat[2].text


def test_edit_first_message_removes_original_brief_and_all_later_chat(ws):
    out = conversations.edit_conversation(ws, ws.load().chat[0].id, "New brief")
    assert ws.load().chat == [] and ws.load().message == ""
    assert out["result"]["edited_message"]["text"] == "New brief"


def test_attachment_only_edit_and_text_limit(ws, tmp_path):
    attach(ws, tmp_path)
    out = conversations.edit_conversation(ws, ws.load().chat[0].id, "")
    assert out["result"]["edited_message"]["attachments"]
    ws.add_chat("human", "Text only")
    with pytest.raises(ValueError, match="text or an attachment"):
        conversations.edit_conversation(ws, ws.load().chat[0].id, " ")
    with pytest.raises(ValueError, match="100,000"):
        conversations.edit_conversation(ws, ws.load().chat[0].id, "x" * 100001)


def test_rewind_idempotency_survives_new_chat_and_does_not_repeat_reset(ws):
    body = payload(ws)
    first = conversations.apply(ws, None, body)
    ws.add_chat("human", "New branch")
    write_json(ws.memory / "lead_session.json", {"threads": {"new": {"thread_id": "new-thread"}}})
    current = ws.request_path.read_bytes()
    second = conversations.apply(ws, None, body)
    assert second["result"]["replayed"]
    assert second["result"]["backup_path"] == first["result"]["backup_path"]
    assert ws.request_path.read_bytes() == current
    assert read_json(ws.memory / "lead_session.json")["threads"]["new"]["thread_id"] == "new-thread"
    with pytest.raises(HistoryConflict, match="different history operation"):
        conversations.apply(ws, None, {**body, "message_id": ws.load().chat[0].id})


def test_fork_idempotency_returns_original_destination_after_new_source_chat(ws):
    body = payload(ws, "fork")
    first = conversations.apply(ws, ws.root.parent / "r-one", body)
    ws.add_chat("human", "Later source change")
    second = conversations.apply(ws, ws.root.parent / "r-two", body)
    assert second["workspace"].root == first["workspace"].root
    assert second["result"]["replayed"] and not (ws.root.parent / "r-two").exists()


@pytest.mark.parametrize("extra,match", [
    ({"expected_revision": 999}, "history changed"),
    ({"expected_revision": True}, "nonnegative"),
    ({"message_id": "missing"}, "not in this conversation"),
    ({"action": "edit", "text": "edited"}, "human message"),
    ({"fork": "false"}, "boolean"),
    ({"operation_id": ""}, "operation_id"),
])
def test_invalid_history_requests_do_not_mutate_chat(ws, extra, match):
    before = ws.request_path.read_bytes()
    with pytest.raises(ValueError, match=match):
        conversations.apply(ws, None, payload(ws, **extra))
    assert ws.request_path.read_bytes() == before


def test_duplicate_message_identity_rejected(ws):
    req = ws.load()
    req.chat[1].id = req.chat[0].id
    ws.save(req)
    with pytest.raises(ValueError, match="unique"):
        conversations.rewind_conversation(ws, req.chat[0].id)


def test_legacy_message_ids_remain_stable_through_fork_and_save(ws):
    row = read_json(ws.request_path)
    for turn in row["chat"]:
        turn.pop("id")
    write_json(ws.request_path, row)
    legacy = ws.load()
    assert [turn.id for turn in legacy.chat] == [turn.id for turn in ws.load().chat]
    fork = conversations.fork_conversation(ws, ws.root.parent / "r-legacy", legacy.chat[1].id)
    ws.save(ws.load())
    assert fork.load().chat == ws.load().chat[:2]


def test_corrupt_original_aborts_before_history_or_destination_change(ws, tmp_path):
    ref = attach(ws, tmp_path)
    original = attachments.original_path(ws, ref)
    original.chmod(0o644)
    original.write_bytes(b"Corrupted")
    before = ws.request_path.read_bytes()
    for target, action in [(None, "rewind"), (ws.root.parent / "r-bad", "fork")]:
        with pytest.raises(ValueError, match="integrity"):
            conversations.apply(ws, target, payload(ws, action))
    assert ws.request_path.read_bytes() == before
    assert not (ws.root.parent / "r-bad").exists()


def test_destination_symlinks_and_private_symlinks_rejected(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        conversations.fork_conversation(ws, alias / "r-fork")
    (ws.root / ".attachments").symlink_to(outside, target_is_directory=True)
    write_json(ws.memory / "plan.json", {"steps": []})
    (ws.memory / "plan.json").unlink()
    (ws.memory / "plan.json").symlink_to(outside / "private")
    with pytest.raises(ValueError, match="symbolic"):
        conversations.rewind_conversation(ws, ws.load().chat[1].id)
    assert not (outside / "private").exists()


def test_work_symlinks_are_not_dereferenced_or_copied(ws, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("Private outside bytes")
    (ws.work / "outside").symlink_to(secret)
    out = conversations.apply(ws, ws.root.parent / "r-fork", payload(ws, "fork"))
    assert not (out["workspace"].work / "outside").exists()
    assert out["result"]["skipped_work_paths"] == ["outside"]


def test_interrupted_reset_is_recoverable_and_blocks_different_mutation(ws, monkeypatch):
    future_state(ws)
    body = payload(ws)
    before = ws.load().model_dump(mode="json")
    real_write = checkpoints.durable_json

    def fail_history_publish(path, value):
        if path == ws.request_path:
            raise OSError("Simulated disk failure")
        return real_write(path, value)

    monkeypatch.setattr(checkpoints, "durable_json", fail_history_publish)
    with pytest.raises(OSError, match="Simulated"):
        conversations.apply(ws, None, body)
    assert ws.load().model_dump(mode="json") == before
    assert not (ws.memory / "providers").exists()
    with pytest.raises(HistoryConflict, match="unfinished"):
        conversations.apply(ws, None, {**body, "operation_id": "different"})
    monkeypatch.setattr(checkpoints, "durable_json", real_write)
    out = conversations.apply(ws, None, body)
    assert out["result"]["replayed"] and len(ws.load().chat) == 2
    backup = Path(out["result"]["backup_path"])
    assert read_json(backup / "request-before.json") == before
    assert (backup / "saved" / "memory" / "notes.md").read_text() == "FUTURE_NOTES"


def test_fork_recovers_publication_when_receipt_write_failed(ws, monkeypatch):
    body = payload(ws, "fork")
    target = ws.root.parent / "r-fork"
    real_write = conversations.durable_json

    def fail_receipt(path, value):
        if path.name == "operation.json" and value.get("status") == "complete":
            raise OSError("Simulated receipt failure")
        return real_write(path, value)

    monkeypatch.setattr(conversations, "durable_json", fail_receipt)
    with pytest.raises(OSError, match="receipt"):
        conversations.apply(ws, target, body)
    assert target.exists()
    ws.add_chat("human", "Source advanced")
    monkeypatch.setattr(conversations, "durable_json", real_write)
    out = conversations.apply(ws, ws.root.parent / "r-other", body)
    assert out["workspace"].root == target and out["result"]["replayed"]


def test_retry_cleans_only_its_abandoned_fork_staging_directory(ws, monkeypatch):
    body = payload(ws, "fork")
    target = ws.root.parent / "r-fork"
    real_copy = conversations._copy_tree

    def fail_copy(*_args, **_kwargs):
        raise OSError("Interrupted copy")

    monkeypatch.setattr(conversations, "_copy_tree", fail_copy)
    with pytest.raises(OSError, match="Interrupted"):
        conversations.apply(ws, target, body)
    record = read_json(checkpoints.operation_path(ws, body["operation_id"]))
    # Simulate a process exit before its finally block could remove staging.
    abandoned = Path(record["staging"])
    abandoned.mkdir()
    (abandoned / "partial.bin").write_bytes(b"Partial copy")
    unrelated = target.parent / ".r-fork-fork-unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("Not this operation")
    monkeypatch.setattr(conversations, "_copy_tree", real_copy)
    out = conversations.apply(ws, target, body)
    assert out["workspace"].root == target and not abandoned.exists()
    assert (unrelated / "keep.txt").read_text() == "Not this operation"


@pytest.mark.parametrize("host", ["codex", "tool_loop", "anthropic_api", "openrouter_api"])
def test_fresh_provider_gets_full_retained_chat_without_future_context(ws, monkeypatch, host):
    req = ws.load()
    req.chat = [ChatTurn(role="human" if i % 2 == 0 else "job_lead", text=f"PUBLIC-{i}-" + "long" * 210)
                for i in range(22)]
    ws.save(req)
    future_state(ws)
    conversations.rewind_conversation(ws, req.chat[19].id)
    ws.add_chat("human", "NEW_MESSAGE")
    monkeypatch.setattr(job_lead, "system_prompt", lambda *_: "System")
    monkeypatch.setattr(job_lead, "_prompt_surface", lambda *_: {})
    seen = []

    def codex(_ws, _registry, _swarm, prompt, _cfg, *, resume, session, **_kwargs):
        assert resume is None
        seen.append(prompt)
        session["thread_id"] = "fresh-thread"
        return "Done"

    def tool_loop(_gateway, _cfg, _system, parts, *_args, **_kwargs):
        seen.append(json.dumps(parts))
        return "Done"

    def post(provider, body, _activity):
        seen.append(json.dumps(body))
        if provider == "anthropic":
            return {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Done"}]}
        return {"choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(job_lead.host_codex, "run_lead", codex)
    monkeypatch.setattr(job_lead.loops, "openai_loop", tool_loop)
    monkeypatch.setattr(provider_loop, "_post", post)
    provider = "anthropic" if host == "anthropic_api" else "openrouter" if host == "openrouter_api" else "openai"
    cfg = {"host": host, "provider": provider, "model": "test", "max_turns": 1, "image_input_supported": True}
    # No cfg.selection, including first_turn=True, must still replay the checkpoint.
    job_lead._run_host(ws, Registry([]), None, "NEW_MESSAGE", True, cfg,
                       Ledger(ws.ledger / "ledger.jsonl"), read_json(ws.memory / "lead_session.json"), [])
    assert seen
    assert req.chat[0].text in seen[0] and req.chat[19].text in seen[0]
    assert "PUBLIC-20-" not in seen[0] and "PUBLIC-21-" not in seen[0]
    assert "FUTURE" not in seen[0] and "NEW_MESSAGE" in seen[0]


def test_run_fences_old_human_events_even_if_session_sequence_is_lost(ws, monkeypatch):
    ledger = Ledger(ws.ledger / "ledger.jsonl")
    ledger.append("human.answer", "human", {"ticket": "old", "text": "OLD_FUTURE_EVENT"})
    conversations.rewind_conversation(ws, ws.load().chat[1].id)
    write_json(ws.memory / "lead_session.json", {"seq": 0})
    ledger.append("human.answer", "human", {"ticket": "new", "text": "NEW_EVENT"})
    ws.add_chat("human", "Next message")
    seen = []

    def lead(_ws, _registry, _swarm, _message, _first, _cfg, _ledger, _session, events):
        seen.extend(events)
        return "Done"

    monkeypatch.setattr(job_lead, "_lead", lead)
    monkeypatch.setattr(job_lead, "Swarm", lambda *_: SimpleNamespace(shutdown=lambda: None))
    monkeypatch.setattr(job_lead, "build_registry", lambda: Registry([]))
    monkeypatch.setattr(job_lead.prompts, "with_snapshot", lambda cfg, *_: cfg)
    job_lead.run(ws, "Next message", False, cfg={"prompt": {}})
    assert "NEW_EVENT" in " ".join(seen) and "OLD_FUTURE_EVENT" not in " ".join(seen)


def test_assistant_role_identity_is_accepted_as_a_message_boundary(ws):
    req = ws.load()
    req.chat[1].role = "assistant"
    ws.save(req)
    out = conversations.rewind_conversation(ws, req.chat[1].id)
    assert out["workspace"].load().chat == req.chat[:2]
    assert read_json(ws.memory / "lead_session.json")["turns"] == 1
    assert job_lead.missed_turns(req.chat, 0)[1].role == "assistant"


def test_empty_latest_fork_and_no_mutation_of_callers_payload(ws):
    req = ws.load()
    req.chat = []
    ws.save(req)
    body = {"action": "fork", "message_id": None, "operation_id": "empty"}
    original = copy.deepcopy(body)
    out = conversations.apply(ws, ws.root.parent / "r-empty", body)
    assert out["workspace"].load().chat == [] and body == original
    assert attachment_inputs.catalog(out["workspace"]) == []
