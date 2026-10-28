"""QC persistence and authority boundaries without paid model calls."""
from __future__ import annotations

import copy
import json
import sys
import time

import numpy as np
import pytest
from filelock import FileLock

from vwb.agents import provider_loop
from vwb.canvas.model import Canvas
from vwb.gateway import Gateway
from vwb.qc.chat import QCWorkspace, configuration, family, model_identity, run
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.util.hashing import sha256_file, sha256_json
from vwb.util.jsonio import read_json, write_json
from vwb.web.queue import TurnQueue
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, ChatTurn, Request


@pytest.fixture
def parent(tmp_path):
    ws = Workspace(tmp_path / "r-video")
    for name in SUBDIRS:
        (ws.root / name).mkdir(parents=True)
    ws.save(Request(id=ws.id, message="EDITOR_BRIEF", chat=[ChatTurn(role="job_lead", text="EDITOR_REASONING_SECRET")]))
    output = "short"
    data = Canvas.model_validate({"request": ws.id, "output": output, "version": 1,
               "format": {"width": 64, "height": 48, "fps": 24}, "duration_frames": 48,
               "time_map": [], "layers": [], "provenance": {"author_model": "gpt-6-astra"}}).dump()
    data["provenance"]["sha256"] = sha256_json({k: v for k, v in data.items() if k != "provenance"})
    write_json(ws.canvas / output / "v1.json", data)
    (ws.canvas / output / "HEAD").write_text("1")
    render = ws.renders / output / "v1" / "short_v1.mp4"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"render-v1")
    write_json(render.parent / "receipt.json", {"output": output, "version": 1, "file": str(render),
               "sha256": sha256_file(render), "frames": 48, "fps": 24,
               "inputs": {"canvas_sha256": data["provenance"]["sha256"], "media": {}}})
    return ws


def reply(text="QC reply", blocks=None, reason="end_turn"):
    return {"id": "msg-qc", "type": "message", "role": "assistant", "stop_reason": reason,
            "content": blocks or [{"type": "text", "text": text}], "usage": {"input_tokens": 20, "output_tokens": 10}}


def test_qc_history_and_logs_are_private_and_resume(parent, monkeypatch):
    calls = []
    def post(provider, body, activity=None):
        calls.append(copy.deepcopy(body))
        return reply("Observed a clipping issue")
    monkeypatch.setattr(provider_loop, "_post", post)
    cfg = configuration(parent)
    before = parent.request_path.read_bytes()
    run(parent, "Check the footnote", cfg=cfg)
    run(parent, "Look more closely", cfg=cfg)
    assert parent.request_path.read_bytes() == before
    assert "EDITOR_REASONING_SECRET" not in json.dumps(calls)
    assert "Observed a clipping issue" in json.dumps(calls[1])
    qws = QCWorkspace(parent)
    assert len(qws.load().chat) == 4
    assert (qws.ledger / "tools.jsonl").exists()
    assert not (parent.ledger / "tools.jsonl").exists()
    names = {tool["name"] for tool in calls[0]["tools"]}
    assert {"qc_view", "qc_item_create", "attachment_read"} <= names
    assert not names & {"file_read", "canvas_commit", "sandbox_run", "agent_spawn", "integration_execute"}


def test_malicious_tool_request_cannot_write_editor(parent, monkeypatch):
    calls = []
    results = iter([reply(blocks=[{"type": "tool_use", "id": "evil", "name": "file_write",
                                  "input": {"path": "owned.txt", "text": "changed"}}], reason="tool_use"), reply()])
    def post(provider, body, activity=None):
        calls.append(copy.deepcopy(body))
        return next(results)
    monkeypatch.setattr(provider_loop, "_post", post)
    run(parent, "Try QC", cfg=configuration(parent))
    assert "may not call file_write" in json.dumps(calls[-1])
    assert not (parent.work / "owned.txt").exists()
    assert not (QCWorkspace(parent).work / "owned.txt").exists()


def test_model_switch_replays_only_qc_conversation(parent, monkeypatch):
    calls = []
    monkeypatch.setattr(provider_loop, "_post", lambda p, b, activity=None: (calls.append(copy.deepcopy(b)) or reply()))
    cfg = configuration(parent)
    run(parent, "First QC", cfg=cfg)
    run(parent, "Second QC", cfg={**cfg, "model": "claude-test-alternate"})
    run(parent, "Third QC", cfg=cfg)
    assert "First QC" in json.dumps(calls[1])
    assert "Second QC" in json.dumps(calls[2])
    assert "EDITOR_REASONING_SECRET" not in json.dumps(calls)


def test_qc_lock_independent_and_concurrent_qc_rejected(parent, monkeypatch):
    monkeypatch.setattr(provider_loop, "_post", lambda *a, **k: reply())
    qws = QCWorkspace(parent)
    qws.initialize()
    with FileLock(str(parent.root / "turn.lock")):
        assert run(parent, "QC during edit", cfg=configuration(parent))["lane"] == "qc"
    with FileLock(str(qws.root / "turn.lock")), pytest.raises(ValueError, match="still answering"):
        run(parent, "Concurrent QC", cfg=configuration(parent))


def test_frames_are_pinned_and_read_only(parent, monkeypatch):
    from vwb.tools import qc_chat_tools
    cfg = configuration(parent)
    qws = QCWorkspace(parent)
    qws.initialize()
    ctx = ToolContext(qws, "qc_lead", "qc_lead", Gateway(qws),
                      state={"qc_parent": parent, "qc_targets": cfg["qc_targets"], "qc_model": cfg["model"]})
    monkeypatch.setattr(qc_chat_tools, "read_frames", lambda path, frames: {f: np.zeros((48, 64, 3), dtype=np.uint8) for f in frames})
    registry = build_registry()
    args = {"target": "output:short@v1", "frame": 5, "region": [0.1, 0.2, 0.5, 0.5]}
    result = registry.call(ctx, "qc_view", args)
    assert not result.is_error, result.text
    assert len(result.images) == 2
    assert result.data["frames"] == [5]
    assert not registry.call(ctx, "qc_frames", {"target": args["target"], "f0": 0, "f1": 48}).is_error
    assert registry.call(ctx, "qc_view", {**args, "frame": 48}).is_error
    assert registry.call(ctx, "qc_view", {**args, "target": "output:short@v2"}).is_error
    assert registry.call(ctx, "qc_view", {**args, "region": [0, 0, float('nan'), 1]}).is_error
    ctx.state["qc_model"] = "gpt-6.1-sol"
    assert "different QC model family" in registry.call(ctx, "qc_view", args).text
    ctx.state["qc_model"] = cfg["model"]
    (parent.renders / "short/v1/short_v1.mp4").write_bytes(b"changed")
    assert "changed" in registry.call(ctx, "qc_view", args).text


def until(predicate):
    end = time.monotonic() + 6
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("Queue did not reach expected state")


def test_edit_and_qc_queue_stop_restart_independently(parent, tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(30)\n")
    qws = QCWorkspace(parent)
    qws.initialize()
    queue = TurnQueue(tmp_path, command=[sys.executable, str(worker)])
    try:
        edit = queue.enqueue(parent, message="Edit")
        qc = queue.enqueue(qws, message="QC", main_config=configuration(parent))
        until(lambda: queue.snapshot(parent)["active"] and queue.snapshot(qws)["active"])
        queue.stop(qws)
        until(lambda: queue.snapshot(qws)["active"] is None)
        assert queue.snapshot(parent)["active"]["id"] == edit["id"]
        assert queue.snapshot(qws)["resumable_job"]["id"] == qc["id"]
        job = read_json(qws.memory / "web_jobs" / f"{qc['id']}.json")
        assert job["lane"] == "qc" and job["parent_request"] == str(parent.root)
        assert "qc_instructions" in job["main_config"] and "prompt" not in job["main_config"]
    finally:
        queue.close()
    restarted = TurnQueue(tmp_path, command=[sys.executable, str(worker)])
    try:
        assert restarted.snapshot(qws)["paused"]
        assert restarted.snapshot(qws)["active"] is None
        restarted.resume(qws, job_id=qc["id"])
        until(lambda: restarted.snapshot(qws)["active"])
        assert restarted.snapshot(parent)["active"] is None
    finally:
        restarted.close()


def test_private_qc_workspace_rejects_symlink(parent, tmp_path):
    (parent.root / ".qc-chat").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        QCWorkspace(parent)


@pytest.mark.parametrize(("model", "expected"), [
    ("google/gemini-2.5-flash:free", "google"),
    ("anthropic/claude-opus-5.5", "anthropic"),
    ("openai/gpt-6-astra:fast", "openai"),
    ("qwen/qwen3", None),
])
def test_family_handles_provider_routes_and_variants(model, expected):
    assert family(model) == expected
    assert model_identity("qwen/qwen3:free") == model_identity("qwen3")


def test_commit_uses_host_model_not_agent_authored_provenance(parent, monkeypatch):
    from vwb.canvas import store
    canvas = Canvas(request=parent.id, output="new", format={"width": 64, "height": 48, "fps": 24},
                    time_map=[], duration_frames=48, provenance={"author_model": "forged-author"})
    monkeypatch.setattr(store, "load_working", lambda *_: canvas)
    monkeypatch.setattr(store, "check", lambda *_: [])
    monkeypatch.setattr(store, "expand", lambda *_: canvas)
    write_json(parent.memory / "active_host.json", {"key": "codex:gpt-6-astra"})
    committed, _ = store.CanvasStore(parent.canvas).commit(parent, "new", "job_lead")
    assert committed.provenance["author_model"] == "gpt-6-astra"
