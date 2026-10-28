"""Durable QC rechecks: pinned comparisons remain private, sampled observations.

The store verifies bytes/metadata, not decoding or receipt authenticity. These
fixtures intentionally use small byte files; the host inspection tests own pixels.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from vwb.canvas.model import Canvas
from vwb.qc.chat_store import QCConflict, QCPinError, QCStore, QCStoreError
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import write_json
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request, SourceRef


def render(ws, version, *, output="clip", content=None, source=None, author_model="editor/model"):
    data = Canvas(request=ws.id, output=output, version=version,
                  format={"width": 32, "height": 24, "fps": 24},
                  duration_frames=8 if source is None else None,
                  time_map=[] if source is None else [{"media": source.ref, "src_in": 0, "src_out": 8}]).dump()
    data["provenance"] = {"author_model": author_model,
                          "sha256": sha256_json({k: v for k, v in data.items() if k != "provenance"})}
    write_json(ws.canvas / output / f"v{version}.json", data)
    path = ws.renders / output / f"v{version}" / "output.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = content if content is not None else f"render-{output}-v{version}".encode()
    path.write_bytes(content)
    write_json(path.parent / "receipt.json", {
        "output": output, "version": version, "file": str(path),
        "sha256": hashlib.sha256(content).hexdigest(), "frames": 8, "fps": 24,
        "inputs": {"canvas_sha256": data["provenance"]["sha256"],
                   "media": {} if source is None else {source.ref: source.sha256}},
    })
    return path


def finding(store, *, lane="shared"):
    return store.create({"target": "output:clip@v1", "f0": 1, "f1": 4,
                         "region": [0.1, 0.2, 0.3, 0.4], "expected": "Unclipped text",
                         "observed": "Clipped text", "lane": lane})


def prepare_args(store, item, **overrides):
    target = store.verify_target("output:clip@v2")
    return {"item_id": item["id"], "target": target["target"], "f0": 2, "f1": 6,
            "expected_revision": item["revision"], "idempotency_key": "recheck-one",
            "pin": {k: target[k] for k in ("sha256", "canvas_file_sha256")},
            "model": "review/model", "provider": "review-provider", **overrides}


def evidence(record):
    return [{"target": row["target"], "sha256": row["sha256"],
             "canvas_file_sha256": row["canvas_file_sha256"], "frames": [row["f0"]],
             "region": row["region"], "coverage": "one frame", "ts": "2026-10-28T00:00:00Z",
             "actor": "qc_lead", "width": 32, "height": 24,
             "metadata": {"future_host_field": [True, None, 0.5]}}
            for row in (record["before"], record["after"])]


def complete_args(record, **overrides):
    return {"id": record["id"], "assessment": "persists", "summary": "Text remains clipped in sampled frames.",
            "evidence": evidence(record), "model": record["model"], "provider": record["provider"], **overrides}


def persisted(record):
    return {k: v for k, v in record.items() if k not in ("stale", "stale_reason")}


def events(ws):
    return [json.loads(line) for line in (ws.root / ".qc-chat/events.jsonl").read_text().splitlines()]


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-recheck")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True)
    workspace.save(Request(id=workspace.id, message="Recheck fixture"))
    render(workspace, 1)
    render(workspace, 2)
    return workspace


@pytest.fixture
def prepared(ws):
    store = QCStore(ws)
    item = finding(store)
    args = prepare_args(store, item)
    return store, item, args, store.prepare_recheck(**args)


def test_prepare_freezes_full_windows_and_finding_without_side_effects(ws, prepared):
    store, item, args, record = prepared
    assert str(UUID(record["id"])) == record["id"]
    assert set(record) == {"id", "idempotency_key", "status", "item_id", "item_revision", "item",
                           "before", "after", "model", "provider", "created_at", "result"}
    assert record["item"] == item
    assert record["item_revision"] == item["revision"] == 1
    assert record["status"] == "prepared" and record["result"] is None
    assert record["before"] == item["anchor"]
    assert record["after"] == {**store.verify_target(args["target"]), "f0": 2, "f1": 6,
                               "region": item["anchor"]["region"]}
    view = store.get_recheck(record["id"])
    assert persisted(view) == record and not view["stale"] and view["stale_reason"] is None
    assert QCStore(ws).list_rechecks() == [view]
    assert store.get(item["id"]) == item and store.list_items("editor") == []
    assert [event["type"] for event in events(ws)] == ["item.created", "recheck.prepared"]
    record["item"]["expected"] = "caller changed"
    record["before"]["region"][0] = 0.9
    view["after"]["region"][1] = 0.9
    args["pin"]["sha256"] = "0" * 64
    assert QCStore(ws).get_recheck(record["id"])["item"] == item
    assert store.get_recheck(record["id"])["after"]["region"] == item["region"]


@pytest.mark.parametrize("changes", [
    {"f0": -1}, {"f0": True}, {"f0": 1.0}, {"f0": "1"}, {"f1": 2}, {"f1": 9}, {"f1": False},
    {"f1": 2**31}, {"f1": float("inf")}, {"expected_revision": True}, {"expected_revision": 0},
    {"expected_revision": 2}, {"idempotency_key": ""}, {"idempotency_key": "x" * 241},
    {"model": None}, {"model": ""}, {"model": "x" * 257}, {"provider": []}, {"provider": "\0"},
    {"target": "output:clip@v1"}, {"target": "output:other@v2"}, {"target": "output:../clip@v2"},
    {"pin": None}, {"pin": {}}, {"pin": {"sha256": "a" * 64}},
    {"pin": {"sha256": "A" * 64, "canvas_file_sha256": "a" * 64}},
    {"pin": {"sha256": "a" * 64, "canvas_file_sha256": "a" * 64, "extra": 1}},
])
def test_prepare_rejects_invalid_inputs_without_journaling(ws, changes):
    store = QCStore(ws)
    item = finding(store)
    args = prepare_args(store, item, **changes)
    initial = events(ws)
    with pytest.raises(QCStoreError):
        store.prepare_recheck(**args)
    assert store.list_rechecks() == [] and events(ws) == initial
    assert store.get(item["id"]) == item


def test_pin_required_and_configuration_not_accepted(ws):
    store = QCStore(ws)
    args = prepare_args(store, finding(store))
    without_pin = {k: v for k, v in args.items() if k != "pin"}
    with pytest.raises(TypeError):
        store.prepare_recheck(**without_pin)
    with pytest.raises(TypeError):
        store.prepare_recheck(**args, config={})


@pytest.mark.parametrize("change", ["bytes", "canvas"])
def test_prepare_fences_viewer_pin_even_when_new_render_is_self_consistent(ws, change):
    store = QCStore(ws)
    args = prepare_args(store, finding(store))
    render(ws, 2, **({"content": b"replacement"} if change == "bytes" else {"author_model": "new/editor"}))
    with pytest.raises(QCPinError, match="viewer pin"):
        store.prepare_recheck(**args)
    assert store.list_rechecks() == []


@pytest.mark.parametrize("change", ["bytes", "canvas"])
def test_prepare_fences_full_original_anchor(ws, change):
    store = QCStore(ws)
    args = prepare_args(store, finding(store))
    render(ws, 1, **({"content": b"replacement"} if change == "bytes" else {"author_model": "new/editor"}))
    with pytest.raises(QCPinError, match="anchor changed"):
        store.prepare_recheck(**args)
    assert store.list_rechecks() == []


@pytest.mark.parametrize("status", ["resolved", "dismissed"])
def test_closed_findings_cannot_prepare_new_rechecks_but_can_retry_old(ws, prepared, status):
    store, item, args, record = prepared
    changed = store.transition(item["id"], status, expected_revision=1)
    assert store.prepare_recheck(**args) == record
    with pytest.raises(QCStoreError, match="Only open"):
        store.prepare_recheck(**{**args, "idempotency_key": "new", "expected_revision": changed["revision"]})
    view = store.get_recheck(record["id"])
    assert view["stale"] and view["stale_reason"] == f"finding_{status}"


def test_needs_recheck_can_prepare_without_advancing_revision(ws):
    store = QCStore(ws)
    item = finding(store)
    updated = store.reconcile()[0]["item"]
    assert updated["status"] == "needs_recheck"
    with pytest.raises(QCConflict):
        store.prepare_recheck(**prepare_args(store, item))
    record = store.prepare_recheck(**prepare_args(store, updated))
    assert record["item"] == updated and store.get(item["id"]) == updated
    assert not store.get_recheck(record["id"])["stale"]


@pytest.mark.parametrize("field", ["item_id", "expected_revision", "target", "f0", "f1", "model", "provider",
                                    "sha256", "canvas_file_sha256"])
def test_idempotency_key_rejects_each_conflicting_payload_field(prepared, field):
    store, _, args, record = prepared
    changes = {"item_id": str(uuid4()), "expected_revision": 2, "target": "output:clip@v3", "f0": 1,
               "f1": 7, "model": "other/model", "provider": "other-provider"}
    conflict = deepcopy(args)
    if field in ("sha256", "canvas_file_sha256"):
        conflict["pin"][field] = "0" * 64
    else:
        conflict[field] = changes[field]
    with pytest.raises(QCConflict, match="Idempotency"):
        store.prepare_recheck(**conflict)
    assert store.prepare_recheck(**args) == record


def test_prepare_retry_survives_revision_and_media_changes(ws, prepared):
    store, item, args, record = prepared
    store.transition(item["id"], "open", expected_revision=1)
    render(ws, 1, content=b"replaced")
    (ws.renders / "clip/v2/output.mp4").unlink()
    assert QCStore(ws).prepare_recheck(**args) == record
    view = store.get_recheck(record["id"])
    assert view["stale"] and view["stale_reason"] == "finding_revision_changed"
    assert persisted(view) == record


@pytest.mark.parametrize("assessment", ["persists", "not_observed", "inconclusive"])
def test_completion_has_fixed_qualification_and_never_changes_finding(ws, prepared, assessment):
    store, item, args, record = prepared
    kwargs = complete_args(record, assessment=assessment)
    completed = store.complete_recheck(**kwargs)
    assert completed["status"] == "completed" and completed["completed_at"] >= record["created_at"]
    assert {k: v for k, v in completed.items() if k not in ("status", "result", "completed_at")} == {
        k: v for k, v in record.items() if k not in ("status", "result")}
    assert completed["result"] == {"assessment": assessment, "summary": kwargs["summary"],
                                    "evidence": kwargs["evidence"], "qualification": "unqualified_visual_review",
                                    "coverage": "sampled_frames_only"}
    assert store.get(item["id"]) == item
    assert persisted(QCStore(ws).get_recheck(record["id"])) == completed
    assert store.prepare_recheck(**args) == completed
    assert store.complete_recheck(**kwargs) == completed
    assert [event["type"] for event in events(ws)] == ["item.created", "recheck.prepared", "recheck.completed"]
    kwargs["evidence"][0]["frames"].append(3)
    completed["result"]["evidence"][0]["metadata"]["future_host_field"].append("caller changed")
    assert store.get_recheck(record["id"])["result"]["evidence"] == evidence(record)


@pytest.mark.parametrize("evidence_kind", ["none", "empty_receipts", "before_only", "after_only"])
@pytest.mark.parametrize("assessment", ["persists", "not_observed"])
def test_substantive_assessment_requires_actual_frames_in_both_windows(prepared, evidence_kind, assessment):
    store, item, _, record = prepared
    receipts = evidence(record)
    receipts = {"none": [], "empty_receipts": [{**r, "frames": []} for r in receipts],
                "before_only": receipts[:1], "after_only": receipts[1:]}[evidence_kind]
    with pytest.raises(QCStoreError, match="both recheck windows"):
        store.complete_recheck(**complete_args(record, assessment=assessment, evidence=receipts))
    assert store.get(item["id"]) == item
    assert store.get_recheck(record["id"])["status"] == "prepared"


def test_inconclusive_allows_no_evidence_and_minimal_receipts_are_supported(ws, prepared):
    store, _, args, record = prepared
    result = store.complete_recheck(**complete_args(record, assessment="inconclusive", evidence=[]))
    assert result["result"]["evidence"] == []
    next_record = store.prepare_recheck(**{**args, "idempotency_key": "minimal"})
    minimal = [{key: receipt[key] for key in ("target", "sha256", "frames")} for receipt in evidence(next_record)]
    assert store.complete_recheck(**complete_args(next_record, evidence=minimal))["result"]["evidence"] == minimal


@pytest.mark.parametrize("change", [
    {"target": "output:clip@v3"}, {"target": "output:other@v1"}, {"sha256": "0" * 64},
    {"sha256": []}, {"canvas_file_sha256": "0" * 64}, {"canvas_file_sha256": None},
    {"frames": [0]}, {"frames": [4]}, {"frames": [True]}, {"frames": [1.0]}, {"frames": [2**31]},
    {"frames": None}, {"frames": "1"}, {"frames": [1] * 65},
    {"region": [0, 0, 0, 1]}, {"region": [0.9, 0, 0.5, 1]}, {"region": [True, 0, 1, 1]},
    {"region": [0, 0, 1]}, {"coverage": "whole video passed"},
    {"extra": float("nan")}, {"extra": float("inf")}, {"extra": 2**100},
    {"extra": b"not JSON"}, {"extra": {1, 2}}, {"extra": "a" * 4097},
    {"extra": [0] * 129}, {"extra": {str(i): 0 for i in range(65)}},
    {"extra": {1: "non-string key"}}, {"extra": "\ud800"}, {"extra": "bad\0text"},
])
def test_forged_or_unbounded_evidence_is_refused_without_state_changes(ws, prepared, change):
    store, item, _, record = prepared
    receipts = evidence(record)
    receipts[0].update(change)
    initial = events(ws)
    with pytest.raises(QCStoreError):
        store.complete_recheck(**complete_args(record, evidence=receipts))
    assert events(ws) == initial and store.get(item["id"]) == item
    assert store.get_recheck(record["id"])["status"] == "prepared"


@pytest.mark.parametrize("kind", ["not_list", "not_object", "missing_target", "missing_hash", "missing_frames",
                                  "too_many", "too_deep", "large_receipt", "large_total", "wide_tree"])
def test_receipt_shape_and_aggregate_limits(prepared, kind):
    store, _, _, record = prepared
    receipts = evidence(record)
    if kind == "not_list":
        receipts = {}
    elif kind == "not_object":
        receipts[0] = []
    elif kind.startswith("missing_"):
        receipts[0].pop({"missing_target": "target", "missing_hash": "sha256", "missing_frames": "frames"}[kind])
    elif kind == "too_many":
        receipts *= 33
    elif kind == "too_deep":
        nested = None
        for _ in range(10):
            nested = [nested]
        receipts[0]["extra"] = nested
    elif kind == "large_receipt":
        receipts[0]["extra"] = ["x" * 4000] * 5
    elif kind == "wide_tree":
        # A compact in-memory graph can expand to a huge JSON tree. Validation
        # must stop before encoding it, not just measure the serialized result.
        nested = None
        for _ in range(6):
            nested = [nested] * 128
        receipts[0]["extra"] = nested
    else:
        for receipt in receipts:
            receipt["extra"] = ["x" * 4000] * 2
        receipts *= 32
    with pytest.raises(QCStoreError):
        store.complete_recheck(**complete_args(record, evidence=receipts))


def test_evidence_for_identical_render_bytes_still_requires_both_target_versions(ws):
    render(ws, 1, content=b"identical")
    render(ws, 2, content=b"identical")
    store = QCStore(ws)
    record = store.prepare_recheck(**prepare_args(store, finding(store)))
    receipts = evidence(record)
    assert receipts[0]["sha256"] == receipts[1]["sha256"]
    with pytest.raises(QCStoreError, match="both recheck windows"):
        store.complete_recheck(**complete_args(record, evidence=[receipts[0], receipts[0]]))


@pytest.mark.parametrize("changes", [
    {"assessment": "passed"}, {"assessment": []}, {"summary": "x" * 4001}, {"summary": ""},
    {"summary": "bad\0text"}, {"summary": None}, {"model": "other/model"}, {"provider": "other-provider"},
])
def test_completion_validates_outcome_text_and_frozen_reviewer(prepared, changes):
    store, _, _, record = prepared
    with pytest.raises(QCStoreError):
        store.complete_recheck(**complete_args(record, **changes))
    assert store.get_recheck(record["id"])["status"] == "prepared"


def test_summary_boundary_and_receipt_frame_bound(prepared):
    store, _, _, record = prepared
    receipts = evidence(record)
    for receipt in receipts:
        receipt.update(frames=receipt["frames"] * 64, region=None, coverage="sampled frames only")
    completed = store.complete_recheck(**complete_args(record, summary="s" * 4000, evidence=receipts))
    assert len(completed["result"]["summary"]) == 4000


@pytest.mark.parametrize("side", [1, 2])
@pytest.mark.parametrize("change", ["bytes", "valid_replacement", "canvas", "missing"])
def test_completion_reverifies_both_full_media_pins(ws, prepared, side, change):
    store, item, _, record = prepared
    path = ws.renders / "clip" / f"v{side}" / "output.mp4"
    if change == "bytes":
        old = path.stat()
        path.write_bytes(b"tampered")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif change == "valid_replacement":
        render(ws, side, content=b"valid replacement")
    elif change == "canvas":
        render(ws, side, author_model="new/editor")
    else:
        path.unlink()
    with pytest.raises(QCPinError):
        store.complete_recheck(**complete_args(record, assessment="inconclusive", evidence=[]))
    assert store.get(item["id"]) == item
    assert store.get_recheck(record["id"])["status"] == "prepared"


@pytest.mark.parametrize("at_completion", [False, True])
@pytest.mark.parametrize("source_side", [1, 2])
def test_source_bytes_pinned_at_prepare_and_completion(ws, at_completion, source_side):
    path = ws.media / "source.mp4"
    path.write_bytes(b"source bytes")
    source = SourceRef(media_id="src1", path=str(path), original_path=str(path),
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    ws.save(Request(id=ws.id, message="source fixture", sources=[source]))
    render(ws, source_side, source=source)
    store = QCStore(ws)
    item = finding(store)
    args = prepare_args(store, item)
    record = store.prepare_recheck(**args) if at_completion else None
    path.write_bytes(b"mutated source")
    with pytest.raises(QCPinError, match="Source file"):
        if at_completion:
            store.complete_recheck(**complete_args(record))
        else:
            store.prepare_recheck(**args)
    assert store.get(item["id"]) == item


@pytest.mark.parametrize("status", ["resolved", "dismissed", "open"])
def test_later_human_decision_does_not_discard_completed_evidence(ws, prepared, status):
    store, item, _, record = prepared
    changed = store.transition(item["id"], status, expected_revision=1)
    completed = store.complete_recheck(**complete_args(record))
    view = store.get_recheck(record["id"])
    assert persisted(view) == completed
    assert view["stale"]
    assert view["stale_reason"] == ("finding_revision_changed" if status == "open" else f"finding_{status}")
    assert store.list_rechecks() == [view]
    assert store.get(item["id"]) == changed and completed["item"] == item
    assert all("stale" not in event["data"].get("recheck", {}) for event in events(ws))


def test_newer_valid_render_changes_only_computed_staleness(ws, prepared):
    store, item, _, record = prepared
    completed = store.complete_recheck(**complete_args(record))
    initial = events(ws)
    render(ws, 9, output="unrelated")
    render(ws, 4)
    (ws.renders / "clip/v4/output.mp4").write_bytes(b"invalid receipt")
    assert not store.get_recheck(record["id"])["stale"]
    render(ws, 3)
    view = store.get_recheck(record["id"])
    assert view["stale"] and view["stale_reason"] == "newer_render_available"
    assert persisted(view) == completed
    assert store.get(item["id"]) == item and events(ws) == initial
    (ws.renders / "clip/v3/receipt.json").unlink()
    assert not store.list_rechecks()[0]["stale"]


def test_completed_retry_is_durable_after_media_disappears_and_conflicts_are_refused(ws, prepared):
    store, _, _, record = prepared
    kwargs = complete_args(record)
    completed = store.complete_recheck(**kwargs)
    (ws.renders / "clip/v1/output.mp4").unlink()
    assert QCStore(ws).complete_recheck(**kwargs) == completed
    for changes in ({"assessment": "not_observed"}, {"summary": "Different conclusion"},
                    {"evidence": [{**r, "extra": "new metadata"} for r in kwargs["evidence"]]}):
        with pytest.raises(QCConflict, match="different result"):
            store.complete_recheck(**{**kwargs, **changes})
    assert len(events(ws)) == 3


def test_editor_only_sees_explicit_findings_never_rechecks(ws):
    store = QCStore(ws)
    public, private = finding(store), finding(store, lane="audit")
    records = [store.prepare_recheck(**prepare_args(store, item, idempotency_key=item["id"]))
               for item in (public, private)]
    for record in records:
        store.complete_recheck(**complete_args(record, summary="PRIVATE_RECHECK_OBSERVATION"))
    assert store.list_items("editor") == []
    batch = store.handoff([public["id"]], lambda **_: {"id": "editor-job"}, "handoff")
    assert batch["items"] == store.list_items("editor") == [public]
    assert store.get(public["id"], "editor") == public
    with pytest.raises(KeyError):
        store.get(private["id"], "editor")
    assert "PRIVATE_RECHECK_OBSERVATION" not in json.dumps(batch)
    assert all(record["id"] not in json.dumps(batch) for record in records)
    assert "rechecks" not in json.dumps(store.list_items())
    assert not (ws.work / ".qc-chat").exists()


@pytest.mark.parametrize("populated", [False, True])
def test_legacy_snapshot_and_journal_without_rechecks_remain_readable(ws, populated):
    store = QCStore(ws)
    item = finding(store) if populated else None
    projection = ws.root / ".qc-chat/items.json"
    journal = ws.root / ".qc-chat/events.jsonl"
    original_log = journal.read_bytes()
    snapshot = json.loads(projection.read_text())
    snapshot.pop("rechecks")
    write_json(projection, snapshot)
    reopened = QCStore(ws)
    assert reopened.list_rechecks() == []
    assert json.loads(projection.read_text())["rechecks"] == {}
    assert journal.read_bytes() == original_log
    if item:
        assert reopened.get(item["id"]) == item
        record = reopened.prepare_recheck(**prepare_args(reopened, item))
        assert reopened.get_recheck(record["id"])["status"] == "prepared"
        assert journal.read_bytes().startswith(original_log)


@pytest.mark.parametrize("stage", ["prepared", "completed"])
def test_journal_recovers_after_projection_write_failure_without_duplicate_events(ws, monkeypatch, stage):
    store = QCStore(ws)
    args = prepare_args(store, finding(store))
    record = store.prepare_recheck(**args) if stage == "completed" else None
    original = store._write_snapshot

    def fail_projection(directory, state):
        if any(r["status"] == stage for r in state["rechecks"].values()):
            raise RuntimeError("simulated projection crash")
        original(directory, state)

    monkeypatch.setattr(store, "_write_snapshot", fail_projection)
    with pytest.raises(RuntimeError, match="projection crash"):
        if stage == "completed":
            store.complete_recheck(**complete_args(record))
        else:
            store.prepare_recheck(**args)
    recovered = QCStore(ws)
    retried = recovered.prepare_recheck(**args) if stage == "prepared" else recovered.complete_recheck(**complete_args(record))
    assert retried["status"] == stage
    assert len(recovered.list_rechecks()) == 1
    assert [event["type"] for event in events(ws)].count(f"recheck.{stage}") == 1
    assert json.loads((ws.root / ".qc-chat/items.json").read_text())["rechecks"][retried["id"]] == retried


def test_missing_projection_replays_completed_record_and_corrupt_journal_fails_closed(ws, prepared):
    store, _, _, record = prepared
    completed = store.complete_recheck(**complete_args(record))
    snapshot = ws.root / ".qc-chat/items.json"
    snapshot.unlink()
    assert persisted(QCStore(ws).get_recheck(record["id"])) == completed
    journal = ws.root / ".qc-chat/events.jsonl"
    raw = journal.read_bytes()
    for bad in (raw.replace(b"persists", b"resolved"), raw[:-1], b""):
        journal.write_bytes(bad)
        with pytest.raises(QCStoreError):
            QCStore(ws)
    journal.write_bytes(raw)
    assert persisted(QCStore(ws).get_recheck(record["id"])) == completed


def test_concurrent_prepare_retries_produce_one_record(ws):
    store = QCStore(ws)
    args = prepare_args(store, finding(store))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: QCStore(ws).prepare_recheck(**args), range(8)))
    assert all(record == results[0] for record in results)
    assert len(store.list_rechecks()) == 1
    assert [event["type"] for event in events(ws)].count("recheck.prepared") == 1


def test_completion_and_human_transition_race_preserves_both_records(ws, prepared):
    store, item, _, record = prepared
    with ThreadPoolExecutor(max_workers=2) as pool:
        reviewed = pool.submit(QCStore(ws).complete_recheck, **complete_args(record))
        changed = pool.submit(QCStore(ws).transition, item["id"], "resolved", expected_revision=1)
        completed, resolved = reviewed.result(), changed.result()
    assert completed["item"] == item
    assert store.get(item["id"]) == resolved
    assert store.get_recheck(record["id"])["stale_reason"] == "finding_resolved"


def test_conflicting_concurrent_completions_have_one_winner(ws, prepared):
    store, _, _, record = prepared

    def complete(assessment):
        try:
            return QCStore(ws).complete_recheck(**complete_args(record, assessment=assessment))
        except QCConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, ["persists", "not_observed"]))
    assert results.count("conflict") == 1
    assert len([event for event in events(ws) if event["type"] == "recheck.completed"]) == 1
    assert store.get_recheck(record["id"])["status"] == "completed"


@pytest.mark.parametrize("path", [".qc-chat/items.json", ".qc-chat/events.jsonl", ".qc-chat/.lock"])
def test_recheck_operations_preserve_private_symlink_guards(ws, prepared, tmp_path, path):
    store, _, args, record = prepared
    victim = tmp_path / "untouched"
    victim.write_bytes(b"untouched")
    private = ws.root / path
    private.unlink()
    private.symlink_to(victim)
    for action in (store.list_rechecks, lambda: store.get_recheck(record["id"]),
                   lambda: store.prepare_recheck(**args), lambda: store.complete_recheck(**complete_args(record))):
        with pytest.raises(QCStoreError):
            action()
    assert victim.read_bytes() == b"untouched"


@pytest.mark.parametrize("side", [1, 2])
def test_completion_cannot_follow_media_symlinks(ws, prepared, tmp_path, side):
    store, _, _, record = prepared
    path = ws.renders / "clip" / f"v{side}" / "output.mp4"
    outside = tmp_path / "outside.mp4"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(QCPinError):
        store.complete_recheck(**complete_args(record))


def test_unknown_recheck_and_invalid_ids_are_explicit(prepared):
    store, _, _, record = prepared
    with pytest.raises(KeyError):
        store.get_recheck(str(uuid4()))
    with pytest.raises(KeyError):
        store.complete_recheck(**complete_args(record, id=str(uuid4())))
    for bad in ("../private", None, 1, str(uuid4()).upper()):
        with pytest.raises(QCStoreError):
            store.get_recheck(bad)
        with pytest.raises(QCStoreError):
            store.complete_recheck(**complete_args(record, id=bad))
