"""Private findings, immutable media pins, and durable explicit handoffs."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from vwb.canvas.model import Canvas
from vwb.qc.chat_store import QCConflict, QCPinError, QCStore, QCStoreError
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import write_json
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request, SourceRef


@pytest.fixture(scope="session")
def movie(tmp_path_factory):
    """Encode once locally: 6 real 32x24 frames, no providers or renderer stack."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for the tiny real rendered fixture")
    path = tmp_path_factory.mktemp("qc-movie") / "fixture.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x24:r=24",
                    "-frames:v", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=30)
    return path.read_bytes()


def render(ws, movie, version=1, output="clip", *, source=None, author_model=None):
    canvas = Canvas(request=ws.id, output=output, version=version,
                    format={"width": 32, "height": 24, "fps": 24},
                    time_map=[] if source is None else [{"media": source.ref, "src_in": 0, "src_out": 6}],
                    duration_frames=6 if source is None else None)
    data = canvas.dump()
    data["provenance"] = {"author": "job_lead", "committed": "2026-10-28T00:00:00Z"}
    if author_model is not None:
        data["provenance"]["author_model"] = author_model
    data["provenance"]["sha256"] = sha256_json({k: v for k, v in data.items() if k != "provenance"})
    write_json(ws.canvas / output / f"v{version}.json", data)
    (ws.canvas / output / "HEAD").write_text(str(version))
    directory = ws.renders / output / f"v{version}"
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{output}_v{version}.mp4"
    final.write_bytes(movie)
    receipt = {"output": output, "version": version, "file": str(final),
               "sha256": hashlib.sha256(movie).hexdigest(), "frames": 6, "fps": 24,
               "inputs": {"canvas_sha256": data["provenance"]["sha256"],
                          "media": {} if source is None else {source.ref: source.sha256}}}
    write_json(directory / "receipt.json", receipt)
    return final


@pytest.fixture
def ws(tmp_path, movie):
    workspace = Workspace(tmp_path / "r-qc")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True)
    workspace.save(Request(id=workspace.id, message="QC fixture"))
    render(workspace, movie)
    return workspace


def payload(**changes):
    return {"target": "output:clip@v1", "f0": 0, "f1": 6,
            "expected": "The title is fully visible.", "observed": "The title is clipped.", **changes}


def events(ws):
    return [json.loads(line) for line in (ws.root / ".qc-chat/events.jsonl").read_text().splitlines()]


class Queue:
    """A durable idempotent queue stub; restart reads the same job file."""

    def __init__(self, ws):
        self.path = ws.root / "queue-fixture.json"
        self.calls = []

    def __call__(self, *, message, idempotency_key):
        self.calls.append((message, idempotency_key))
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        if idempotency_key not in saved:
            saved[idempotency_key] = {"id": f"job-{len(saved) + 1}", "message": message, "status": "queued"}
            write_json(self.path, saved)
        assert saved[idempotency_key]["message"] == message
        return deepcopy(saved[idempotency_key])


def test_create_pins_real_render_and_detaches_results(ws, movie):
    store = QCStore(ws)
    target = store.targets()[0]
    assert {key: target[key] for key in ("target", "output", "version", "fps", "width", "height", "frames")} == {
        "target": "output:clip@v1", "output": "clip", "version": 1, "fps": 24, "width": 32, "height": 24, "frames": 6}
    assert target["sha256"] == hashlib.sha256(movie).hexdigest()
    assert target["author_model"] is None
    item = store.create(payload(region=[0.1, 0.2, 0.3, 0.4]), actor="qc_lead", model="test/model")
    assert str(UUID(item["id"])) == item["id"]
    assert item["revision"] == 1 and item["status"] == "open"
    assert item["created_at"] == item["opened_at"] == item["updated_at"]
    assert item["closed_at"] is None
    assert item["anchor"] == {**target, "f0": 0, "f1": 6, "region": [0.1, 0.2, 0.3, 0.4]}
    assert item["actor"] == "qc_lead" and item["model"] == "test/model"
    original = deepcopy(item)
    item["anchor"]["sha256"] = "changed"
    assert QCStore(ws).get(item["id"]) == original
    assert events(ws)[0]["model"] == "test/model"
    assert not (ws.work / ".qc-chat").exists()


def test_targets_all_versions_author_model_bound_and_missing_render(ws, movie):
    render(ws, movie, version=2, author_model="provider/editor")
    render(ws, movie, version=3)
    (ws.renders / "clip/v3/receipt.json").unlink()
    store = QCStore(ws)
    assert [t["version"] for t in store.targets()] == [2, 1]
    assert store.targets(limit=1)[0]["author_model"] == "provider/editor"
    for bad in (0, 1001, True, 1.0):
        with pytest.raises(QCStoreError):
            store.targets(limit=bad)


def test_target_cap_does_not_hide_recheck_for_existing_finding(ws, movie):
    store = QCStore(ws)
    item = store.create(payload())
    render(ws, movie, version=2)
    for version in range(3, 1003):
        render(ws, movie, version=version, output="busy")
    targets = store.targets()
    assert len(targets) == 1000 and all(target["output"] == "busy" for target in targets)
    candidates = store.reconcile()
    assert candidates[0]["item"]["id"] == item["id"]
    assert candidates[0]["current_target"]["target"] == "output:clip@v2"


@pytest.mark.parametrize("changes", [
    {"unknown": "field"}, {"id": "authored"}, {"status": "resolved"}, {"anchor": {}}, {"model": "forged"},
    {"f0": True}, {"f0": 0.0}, {"f0": -1}, {"f0": "0"}, {"f1": 0}, {"f1": 7}, {"f1": float("inf")},
    {"f1": float("nan")}, {"expected": ""}, {"expected": 12}, {"observed": " "}, {"expected": "a" * 4001},
    {"expected": "bad\0text"}, {"expected": "\ud800"}, {"category": "bad/category"}, {"category": []},
    {"priority": "critical"}, {"priority": []}, {"lane": "editor"}, {"lane": []},
    {"region": []}, {"region": [0, 0, 1]}, {"region": "0,0,1,1"}, {"region": [False, 0, 1, 1]},
    {"region": [0, 0, float("nan"), 1]}, {"region": [0, 0, float("inf"), 1]}, {"region": [0, 0, 0, 1]},
    {"region": [-0.1, 0, 1, 1]}, {"region": [0.5, 0, 0.6, 1]}, {"region": [0, 0.5, 1, 0.6]},
    {"target": "output:../clip@v1"}, {"target": "output:clip@v01"}, {"target": "output:clip@v0"},
    {"target": "output:/clip@v1"}, {"target": "output:clip\\other@v1"}, {"target": "output:%2e%2e@v1"},
    {"target": "output:clip@v1/../../work"}, {"target": "source:src1@abc"}, {"target": []},
    {"pin": None}, {"pin": {}}, {"pin": {"sha256": "a" * 64}},
    {"pin": {"sha256": "A" * 64, "canvas_file_sha256": "a" * 64}},
    {"pin": {"sha256": "g" * 64, "canvas_file_sha256": "a" * 64}},
    {"pin": {"sha256": "a" * 64, "canvas_file_sha256": "a" * 64, "extra": 1}},
])
def test_strict_input_validation_is_nonmutating(ws, changes):
    store = QCStore(ws)
    with pytest.raises(QCStoreError):
        store.create(payload(**changes))
    assert store.list_items() == []
    assert events(ws) == []


def test_required_fields_and_provenance(ws):
    store = QCStore(ws)
    for field in ("target", "f0", "f1", "expected", "observed"):
        data = payload()
        data.pop(field)
        with pytest.raises(QCStoreError):
            store.create(data)
    for data in (None, [], "payload"):
        with pytest.raises(QCStoreError):
            store.create(data)
    for kwargs in ({"actor": ""}, {"actor": 7}, {"model": []}):
        with pytest.raises(QCStoreError):
            store.create(payload(), **kwargs)


def test_create_optional_pin_rejects_valid_rerender_since_inspection(ws, movie):
    store = QCStore(ws)
    target = store.targets()[0]
    pin = {k: target[k] for k in ("sha256", "canvas_file_sha256")}
    first = store.create(payload(pin=pin))
    render(ws, movie + b"changed render")  # A valid new receipt for the same target.
    with pytest.raises(QCPinError, match="stale"):
        store.create(payload(pin=pin))
    assert len(store.list_items()) == 1
    with pytest.raises(QCPinError, match="anchor changed"):
        store.handoff([first["id"]], Queue(ws), "stale")
    fresh = store.verify_target(target["target"])
    assert store.create(payload(pin={k: fresh[k] for k in pin}))["anchor"]["sha256"] == fresh["sha256"]


def test_pin_fences_canvas_provenance_change(ws, movie):
    store = QCStore(ws)
    target = store.targets()[0]
    render(ws, movie, author_model="new-model")
    with pytest.raises(QCPinError, match="stale"):
        store.create(payload(pin={k: target[k] for k in ("sha256", "canvas_file_sha256")}))


@pytest.mark.parametrize("kind", ["bytes", "canvas", "receipt_canvas", "frames", "fps", "output", "width"])
def test_stale_or_inconsistent_render_rejected(ws, kind):
    store = QCStore(ws)
    store.targets()  # Warm cache before replacing the file/metadata.
    directory = ws.renders / "clip/v1"
    if kind == "bytes":
        path = directory / "clip_v1.mp4"
        old = path.stat()
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))  # ctime still invalidates cache.
    elif kind == "canvas":
        path = ws.canvas / "clip/v1.json"
        canvas = json.loads(path.read_text())
        canvas["format"]["width"] = 64
        write_json(path, canvas)
    else:
        path = directory / "receipt.json"
        receipt = json.loads(path.read_text())
        if kind == "receipt_canvas":
            receipt["inputs"]["canvas_sha256"] = "a" * 64
        else:
            receipt[kind] = {"frames": 7, "fps": float("nan"), "output": "other", "width": 64}[kind]
        write_json(path, receipt)
    with pytest.raises(QCPinError):
        store.create(payload())
    with pytest.raises(QCPinError):
        store.targets()


def test_hash_cache_is_used_for_poll_but_not_creation_or_inspection(ws, monkeypatch):
    store = QCStore(ws)
    expected = store.targets()[0]
    import vwb.qc.chat_store as module

    # Poison just the stat cache: reads may use it, fresh checks must ignore it.
    path = ws.renders / "clip/v1/clip_v1.mp4"
    identity = module._identity(path.stat())
    monkeypatch.setitem(module._HASH_CACHE, identity, "0" * 64)
    with pytest.raises(QCPinError):
        store.targets()
    assert store.verify_target(expected["target"]) == expected
    monkeypatch.setitem(module._HASH_CACHE, identity, "0" * 64)
    assert store.create(payload())["anchor"]["sha256"] == expected["sha256"]


def test_source_pin_uses_receipt_and_time_map_contract(ws, movie):
    source_file = ws.media / "src1.mp4"
    source_file.write_bytes(movie)
    source = SourceRef(media_id="src1", path=str(source_file), original_path=str(source_file),
                       sha256=hashlib.sha256(movie).hexdigest())
    ws.save(Request(id=ws.id, message="source fixture", sources=[source]))
    render(ws, movie, source=source)
    store = QCStore(ws)
    item = store.create(payload())
    assert item["anchor"]["sources"] == [{"ref": source.ref, "sha256": source.sha256, "file": "media/src1.mp4"}]
    source_file.write_bytes(movie + b"tampered")
    with pytest.raises(QCPinError, match="Source file"):
        store.verify_target(item["target"])


def test_unreceipted_video_layer_does_not_invent_source_requirement(ws, movie):
    path = ws.canvas / "clip/v1.json"
    data = json.loads(path.read_text())
    data["layers"] = [{"id": "source", "type": "video", "source": "src1@notreceipted", "box": [0, 0, 32, 24]}]
    data["provenance"]["sha256"] = sha256_json({k: v for k, v in data.items() if k != "provenance"})
    write_json(path, data)
    path = ws.renders / "clip/v1/receipt.json"
    receipt = json.loads(path.read_text())
    receipt["inputs"]["canvas_sha256"] = data["provenance"]["sha256"]
    write_json(path, receipt)
    assert QCStore(ws).create(payload())["anchor"]["sources"] == []


def test_transitions_require_revision_keep_anchor_and_append_explicit_events(ws):
    store = QCStore(ws)
    first = store.create(payload())
    initial_log = (ws.root / ".qc-chat/events.jsonl").read_bytes()
    with pytest.raises(TypeError):
        store.transition(first["id"], "resolved")
    with pytest.raises(QCConflict):
        store.transition(first["id"], "resolved", expected_revision=2)
    with pytest.raises(QCStoreError):
        store.transition(first["id"], "passed", expected_revision=1)
    with pytest.raises(QCStoreError):
        store.transition(first["id"], "resolved", expected_revision=True)
    item = store.transition(first["id"], "resolved", expected_revision=1, actor="qc_lead",
                            model="review/model", note="Explicit inspection completed")
    assert item["revision"] == 2 and item["closed_at"]
    item = store.transition(first["id"], "open", expected_revision=2, note="Need another look")
    assert item["status"] == "open" and item["closed_at"] is None and item["revision"] == 3
    item = store.transition(first["id"], "dismissed", expected_revision=3)
    assert item["status"] == "dismissed" and item["anchor"] == first["anchor"]
    assert (ws.root / ".qc-chat/events.jsonl").read_bytes().startswith(initial_log)
    assert events(ws)[1]["data"]["note"] == "Explicit inspection completed"
    assert events(ws)[1]["actor"] == "qc_lead" and events(ws)[1]["model"] == "review/model"
    assert QCStore(ws).get(first["id"]) == item


def test_new_versions_only_require_recheck_never_pass_or_move_anchor(ws, movie):
    store = QCStore(ws)
    first = store.create(payload())
    closed = store.create(payload())
    store.transition(closed["id"], "resolved", expected_revision=1)
    queue = Queue(ws)
    store.handoff([first["id"]], queue, "before-update")
    assert store.reconcile() == []
    render(ws, movie, version=2)
    candidates = store.reconcile()
    assert len(candidates) == 1
    assert candidates[0]["current_target"]["target"] == "output:clip@v2"
    changed = candidates[0]["item"]
    assert changed["status"] == "needs_recheck" and changed["revision"] == 2
    assert changed["anchor"] == first["anchor"] and changed["target"] == first["target"]
    assert changed["closed_at"] is None
    size = len(events(ws))
    assert store.reconcile() == candidates and len(events(ws)) == size
    render(ws, movie, version=3)
    assert store.reconcile()[0]["item"]["revision"] == 3
    assert store.get(closed["id"])["status"] == "resolved"
    assert store.get(first["id"], audience="editor") == first
    assert all(e["type"] != "item.passed" for e in events(ws))


def test_editor_visibility_is_explicit_shared_frozen_snapshots_only(ws):
    store = QCStore(ws)
    shared = store.create(payload())
    audit = store.create(payload(lane="audit", observed="private audit detail"))
    assert len(store.list_items()) == 2 and store.list_items("editor") == []
    for id in (shared["id"], audit["id"]):
        with pytest.raises(KeyError):
            store.get(id, "editor")
    with pytest.raises(QCStoreError, match="audit"):
        store.handoff([shared["id"], audit["id"]], Queue(ws), "sealed")
    assert len(events(ws)) == 2
    queue = Queue(ws)
    batch = store.handoff([shared["id"]], queue, "shared", expected_revisions={shared["id"]: 1})
    assert batch["items"] == [shared] and batch["job"]["id"] == "job-1"
    assert batch["id"] in batch["message"] and shared["id"] in batch["message"]
    for text in (shared["expected"], shared["observed"], shared["anchor"]["sha256"]):
        assert text in batch["message"]
    assert "private audit detail" not in batch["message"]
    store.transition(shared["id"], "resolved", expected_revision=1, note="private subsequent note")
    assert store.list_items("editor") == [shared]
    batch["items"][0]["expected"] = "mutated caller copy"
    assert store.get(shared["id"], "editor") == shared
    assert QCStore(ws).get(shared["id"], "editor") == shared
    with pytest.raises(QCStoreError):
        store.list_items("unknown")


def test_handoff_revision_conflict_and_key_conflict(ws):
    store = QCStore(ws)
    first, second = store.create(payload()), store.create(payload())
    queue = Queue(ws)
    with pytest.raises(QCConflict):
        store.handoff([first["id"]], queue, "key", expected_revisions={first["id"]: 2})
    assert queue.calls == []
    batch = store.handoff([first["id"]], queue, "key")
    assert store.handoff([first["id"]], queue, "key") == batch
    assert len(queue.calls) == 1
    with pytest.raises(QCConflict):
        store.handoff([second["id"]], queue, "key")
    with pytest.raises(QCConflict):
        store.handoff([first["id"]], queue, "key", expected_revisions={first["id"]: 1})


def test_handoff_is_durable_before_enqueue_and_callback_can_read_store(ws):
    store = QCStore(ws)
    item = store.create(payload())
    queue = Queue(ws)

    def enqueue(**kwargs):
        saved = json.loads((ws.root / ".qc-chat/items.json").read_text())
        prepared = saved["batches"]["key"]
        assert prepared["status"] == "prepared" and prepared["items"] == [item]
        assert prepared["message"] == kwargs["message"]
        assert QCStore(ws).list_items("editor") == []
        return queue(**kwargs)

    assert store.handoff([item["id"]], enqueue, "key")["status"] == "sent"


def test_retry_after_enqueue_success_but_journal_append_failed(ws, monkeypatch, movie):
    store = QCStore(ws)
    item = store.create(payload())
    queue = Queue(ws)
    append = store._append

    def fail_sent(directory, state, kind, data, actor, model=None):
        if kind == "handoff.sent":
            raise RuntimeError("simulated crash after enqueue")
        return append(directory, state, kind, data, actor, model)

    monkeypatch.setattr(store, "_append", fail_sent)
    with pytest.raises(RuntimeError, match="crash"):
        store.handoff([item["id"]], queue, "retry")
    assert len(queue.calls) == 1 and store.list_items("editor") == []
    store.transition(item["id"], "dismissed", expected_revision=1, note="later private chatter")
    render(ws, movie + b"replacement")
    restarted_queue = Queue(ws)
    batch = QCStore(ws).handoff([item["id"]], restarted_queue, "retry")
    assert restarted_queue.calls == queue.calls
    assert batch["job"]["id"] == "job-1" and batch["items"] == [item]
    assert store.get(item["id"], "editor") == item
    assert len(json.loads(queue.path.read_text())) == 1


def test_retry_after_sent_journal_durable_but_snapshot_failed(ws, monkeypatch):
    store = QCStore(ws)
    item = store.create(payload())
    queue = Queue(ws)
    write = store._write_snapshot

    def fail_sent(directory, state):
        if any(b["status"] == "sent" for b in state["batches"].values()):
            raise RuntimeError("simulated snapshot failure")
        return write(directory, state)

    monkeypatch.setattr(store, "_write_snapshot", fail_sent)
    with pytest.raises(RuntimeError, match="snapshot"):
        store.handoff([item["id"]], queue, "retry")
    batch = QCStore(ws).handoff([item["id"]], queue, "retry")
    assert batch["job"]["id"] == "job-1" and len(queue.calls) == 1
    snapshot = json.loads((ws.root / ".qc-chat/items.json").read_text())
    assert snapshot["batches"]["retry"]["status"] == "sent"


def test_failed_enqueue_can_retry_same_prepared_snapshot(ws):
    store = QCStore(ws)
    item = store.create(payload())

    def fail(**kwargs):
        raise RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError):
        store.handoff([item["id"]], fail, "retry")
    assert store.list_items("editor") == []
    assert QCStore(ws).handoff([item["id"]], Queue(ws), "retry")["items"] == [item]


def _process_create(args):
    root, number = args
    return QCStore(Workspace(Path(root))).create(payload(observed=f"Finding {number}"))["id"]


def test_concurrent_process_writes_preserve_every_item_and_event(ws):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        ids = list(pool.map(_process_create, [(str(ws.root), number) for number in range(12)]))
    assert len(set(ids)) == 12
    assert {item["id"] for item in QCStore(ws).list_items()} == set(ids)
    assert [e["sequence"] for e in events(ws)] == list(range(1, 13))


def test_transient_create_race_retries_with_same_directory_guards(ws, monkeypatch):
    real_open = os.open
    calls = []
    def open_with_race(path, flags, mode=0o777, *, dir_fd=None):
        if path == ".lock":
            calls.append((flags, mode, dir_fd))
            if len(calls) == 1:
                raise FileNotFoundError("Concurrent creator")
        return real_open(path, flags, mode, dir_fd=dir_fd)
    monkeypatch.setattr(os, "open", open_with_race)
    QCStore(ws)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0] & os.O_NOFOLLOW


def test_concurrent_transition_has_one_winner_and_handoff_enqueues_once(ws):
    item = QCStore(ws).create(payload())

    def transition(_):
        try:
            return QCStore(ws).transition(item["id"], "resolved", expected_revision=1)["revision"]
        except QCConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(transition, range(4)))
    assert results.count(2) == 1 and results.count("conflict") == 3
    queue = Queue(ws)
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = list(pool.map(lambda _: QCStore(ws).handoff([item["id"]], queue, "one"), range(4)))
    assert len({b["id"] for b in batches}) == 1 and len(queue.calls) == 1


@pytest.mark.parametrize("child", [".qc-chat", ".qc-chat/items.json", ".qc-chat/events.jsonl", ".qc-chat/.lock",
                                   ".qc-chat/.handoff.lock"])
def test_private_paths_reject_symlinks(ws, tmp_path, child):
    store = QCStore(ws)
    item = store.create(payload())
    victim = tmp_path / "victim"
    victim.write_text("do not touch")
    path = ws.root / child
    if path.is_dir():
        shutil.rmtree(path)
        target = tmp_path / "outside"
        target.mkdir()
    else:
        path.unlink(missing_ok=True)
        target = victim
    path.symlink_to(target)
    with pytest.raises(QCStoreError):
        if child.endswith(".handoff.lock"):
            store.handoff([item["id"]], Queue(ws), "bad")
        else:
            QCStore(ws).list_items()
    assert victim.read_text() == "do not touch"


@pytest.mark.parametrize("child", ["canvas", "canvas/clip", "canvas/clip/v1.json", "renders", "renders/clip",
                                   "renders/clip/v1", "renders/clip/v1/receipt.json", "renders/clip/v1/clip_v1.mp4"])
def test_media_paths_reject_symlinks_at_every_component(ws, tmp_path, child):
    store = QCStore(ws)
    path = ws.root / child
    outside = tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(QCPinError):
        store.create(payload())


@pytest.mark.parametrize("authored", ["../outside.mp4", "renders/clip/v1/../v1/clip_v1.mp4",
                                      "renders/clip/v1/./clip_v1.mp4", "work/render.mp4", "/etc/passwd",
                                      "renders/clip/v2/clip_v2.mp4", "renders\\clip\\v1\\clip_v1.mp4"])
def test_receipt_rejects_traversal_and_outside_paths(ws, authored):
    path = ws.renders / "clip/v1/receipt.json"
    receipt = json.loads(path.read_text())
    receipt["file"] = authored
    write_json(path, receipt)
    with pytest.raises(QCPinError):
        QCStore(ws).create(payload())


def test_private_hardlinks_and_root_symlinks_rejected(ws, tmp_path):
    QCStore(ws)
    alias = tmp_path / "alias"
    alias.symlink_to(ws.root, target_is_directory=True)
    with pytest.raises(QCStoreError):
        QCStore(Workspace(alias))
    path = ws.root / ".qc-chat/items.json"
    os.link(path, tmp_path / "linked-items")
    with pytest.raises(QCStoreError):
        QCStore(ws)


def test_missing_projection_rebuilt_but_corrupt_or_missing_journal_fails_closed(ws):
    store = QCStore(ws)
    item = store.create(payload())
    path = ws.root / ".qc-chat/items.json"
    path.unlink()
    assert QCStore(ws).get(item["id"]) == item
    journal = ws.root / ".qc-chat/events.jsonl"
    raw = journal.read_bytes()
    journal.write_bytes(raw.replace(b"clipped", b"changed"))
    with pytest.raises(QCStoreError, match="integrity"):
        QCStore(ws)
    journal.write_bytes(raw[:-1])
    with pytest.raises(QCStoreError, match="Incomplete"):
        QCStore(ws)
    journal.unlink()
    with pytest.raises(QCStoreError, match="journal missing"):
        QCStore(ws)
