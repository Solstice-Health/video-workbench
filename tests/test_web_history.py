"""Historical requests remain in place and continue through the normal UI queue."""

import sys
import time
from pathlib import Path

import pytest

from vwb.util.jsonio import read_json, write_json
from vwb.web.history import register_request, resolve_request
from vwb.web.queue import QueueConflict, TurnQueue
from vwb.web.server import Workbench
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, ChatTurn, Request, SourceRef


def workspace(parent: Path, rid: str = "r-history") -> Workspace:
    ws = Workspace(parent / rid)
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=rid, message="Create ten shorts"))
    return ws


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    pytest.fail("Historical request job did not finish")


def test_registration_restores_chat_outputs_and_native_thread_without_copying(tmp_path):
    nested = workspace(tmp_path / "benchmarks" / "main" / "runs")
    direct = workspace(tmp_path, "r-normal")
    hidden = workspace(tmp_path / "benchmarks" / "feature" / "runs", "r-unregistered")
    source = nested.media / "source.mp4"
    source.write_bytes(b"source bytes")
    req = nested.load()
    req.chat = [ChatTurn(role=role, text=text) for role, text in [
        ("human", "Create ten shorts"), ("job_lead", "Created ten"),
        ("human", "Enlarge short three"), ("job_lead", "Updated short three")]]
    req.sources = [SourceRef(media_id="src1", path=str(source), original_path=str(source), sha256="a" * 64)]
    nested.save(req)
    write_json(nested.memory / "lead_session.json", {
        "key": "codex:gpt-6-astra", "threads": {"codex:gpt-6-astra": {"thread_id": "original-thread"}}})
    for n in range(1, 11):
        out = f"short-{n:02}"
        (nested.canvas / out).mkdir()
        (nested.canvas / out / "HEAD").write_text("3")
        write_json(nested.canvas / out / "v3.json", {"format": {"width": 1080, "height": 1920}})
        video = nested.renders / out / "v3" / f"{out}_v3.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"render bytes")
    preserved = {p: p.read_bytes() for p in [nested.request_path, source, nested.memory / "lead_session.json"]}
    app = Workbench(tmp_path)
    try:
        assert {r["id"] for r in app.requests()} == {direct.id}
        assert register_request(tmp_path, nested.root) == nested.id
        assert register_request(tmp_path, nested.root) == nested.id  # idempotent
        rows = {r["id"]: r for r in app.requests()}
        assert set(rows) == {direct.id, nested.id} and hidden.id not in rows
        assert rows[nested.id]["output_count"] == 10
        restored = app.workspace(nested.id)
        snapshot = app.snapshot(restored)
        assert restored.root == nested.root
        assert snapshot["request"]["chat"] == req.model_dump(mode="json")["chat"]
        assert snapshot["usage"]["thread_id"] == "original-thread"
        assert all(out["video_url"] for out in snapshot["outputs"])
        assert app.file(nested.id, "media/source.mp4") == source
        with pytest.raises(ValueError):
            app.file(nested.id, "memory/lead_session.json")
        assert not (tmp_path / nested.id).exists()
    finally:
        app.queue.close()
    restarted = Workbench(tmp_path)
    try:
        assert restarted.workspace(nested.id).root == nested.root
    finally:
        restarted.queue.close()
    assert all(p.read_bytes() == value for p, value in preserved.items())


def test_registered_request_queue_dispatches_and_stops_at_original_location(tmp_path):
    ws = workspace(tmp_path / "benchmarks" / "main" / "runs")
    register_request(tmp_path, ws.root)
    worker = tmp_path / "fake_worker.py"
    worker.write_text("""import json, pathlib, sys, time
job = json.loads(pathlib.Path(sys.argv[2]).read_text())
root = pathlib.Path(job['request'])
(root / 'executed.json').write_text(json.dumps(job))
if job['message'] == 'hold': time.sleep(30)
""")
    queue = TurnQueue(tmp_path, command=[sys.executable, str(worker)])
    try:
        queue.enqueue(ws, message="followup")
        wait_for(lambda: (queue.snapshot(ws)["last_job"] or {}).get("status") == "completed")
        assert read_json(ws.root / "executed.json")["request"] == str(ws.root)
        other = workspace(tmp_path / "elsewhere")
        with pytest.raises(QueueConflict):
            queue.snapshot(other)
        queue.enqueue(ws, message="hold")
        wait_for(lambda: read_json(ws.root / "executed.json").get("message") == "hold")
    finally:
        queue.close()
    state = read_json(ws.memory / "web_queue.json")
    assert [job["status"] for job in state["jobs"]] == ["completed", "stopped"]
    assert not (tmp_path / ws.id).exists()


def test_registration_rejects_duplicate_request_ids(tmp_path):
    first = workspace(tmp_path / "one")
    second = workspace(tmp_path / "two")
    register_request(tmp_path, first.root)
    with pytest.raises(ValueError, match="already registered"):
        register_request(tmp_path, second.root)
    workspace(tmp_path)
    with pytest.raises(ValueError, match="already uses"):
        register_request(tmp_path, first.root)


@pytest.mark.parametrize("relative", ["../r-history", "/outside/r-history", ".keys/r-history", "x\\r-history"])
def test_registry_cannot_expand_beyond_allowed_paths(tmp_path, relative):
    write_json(tmp_path / ".ui/history.json", {"schema": 1, "requests": {"r-history": relative}})
    with pytest.raises(ValueError):
        resolve_request(tmp_path, "r-history")


def test_registration_rejects_symlinks_mismatched_ids_and_external_roots(tmp_path):
    runs = tmp_path / "runs"
    outside = workspace(tmp_path / "outside")
    runs.mkdir()
    with pytest.raises(ValueError, match="outside"):
        register_request(runs, outside.root)
    (runs / "alias").symlink_to(outside.root.parent)
    with pytest.raises(ValueError, match="symlink"):
        register_request(runs, runs / "alias" / outside.id)
    nested = workspace(runs / "nested")
    req = nested.load()
    req.id = "r-other"
    nested.save(req)
    with pytest.raises(ValueError, match="does not match"):
        register_request(runs, nested.root)
