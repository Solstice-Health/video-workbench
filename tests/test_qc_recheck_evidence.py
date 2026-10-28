"""Failed image results are not observations; accepted reports are durable."""
from __future__ import annotations

import copy
import json

import pytest
import test_qc_recheck_runtime as fixtures

from vwb.agents import provider_loop
from vwb.gateway import Gateway
from vwb.qc.chat_store import QCStore
from vwb.qc.recheck import RecheckWorkspace, run, targets
from vwb.tools import build_registry, qc_chat_tools
from vwb.tools.base import ToolContext, ToolResult

prepared = fixtures.prepared


@pytest.fixture
def inspection(prepared):
    parent, store, record, cfg = prepared
    ws = RecheckWorkspace(parent, record["id"])
    ctx = ToolContext(ws, "qc_rechecker", "qc_rechecker", Gateway(ws, actor="qc_rechecker"), state={
        "qc_parent": parent, "qc_targets": targets(record), "qc_model": cfg["model"],
        "qc_recheck": record, "qc_inspections": [], "provider_response_index": 0,
    })
    return ctx, build_registry(), store, record


def inspect(ctx, registry, record, tool="qc_frames"):
    results = []
    for side in ("before", "after"):
        window = record[side]
        args = {"target": window["target"]}
        if tool == "qc_frames":
            args.update(f0=window["f0"], f1=window["f1"], count=2)
        else:
            args["frame"] = window["f0"]
            if tool == "qc_view_crop":
                args["region"] = [0.1, 0.1, 0.5, 0.5]
        results.append(registry.call(ctx, "qc_view" if tool == "qc_view_crop" else tool, args))
    return results


def report(ctx, registry):
    return registry.call(ctx, "qc_recheck_report", {
        "assessment": "not_observed", "summary": "No clipping observed in the inspected newer frames.",
    })


@pytest.mark.parametrize("tool", ["qc_frames", "qc_view", "qc_view_crop"])
@pytest.mark.parametrize("failure", ["encoding", "result_construction"])
def test_failed_inspections_cannot_supply_receipts_or_unlock_a_report(inspection, monkeypatch, tool, failure):
    ctx, registry, store, record = inspection
    real_encode = qc_chat_tools.jpeg_bytes
    encodes = 0

    def encode(pixels):
        nonlocal encodes
        encodes += 1
        # Also cover a full frame that succeeds before its crop fails.
        if tool == "qc_view_crop" and encodes % 2:
            return real_encode(pixels)
        raise ValueError("Synthetic encoding failure")

    def result_failure(*args, **kwargs):
        raise ValueError("Synthetic result construction failure")

    with monkeypatch.context() as patch:
        if failure == "encoding":
            patch.setattr(qc_chat_tools, "jpeg_bytes", encode)
        else:
            patch.setattr(ToolResult, "json", staticmethod(result_failure))
        results = inspect(ctx, registry, record, tool)
    assert all(result.is_error and not result.images for result in results)
    assert ctx.state["qc_inspections"] == []
    path = ctx.ws.memory / "qc_inspections.jsonl"
    assert not path.exists() or not path.read_text()
    ctx.state["provider_response_index"] = 1
    rejected = report(ctx, registry)
    assert rejected.is_error and "Inspect both selected windows" in rejected.text
    assert "qc_recheck_report" not in ctx.state
    assert store.get_recheck(record["id"])["status"] == "prepared"


@pytest.mark.parametrize("tool", ["qc_frames", "qc_view", "qc_view_crop"])
def test_success_records_once_and_only_a_later_response_can_report(inspection, tool):
    ctx, registry, store, record = inspection
    original_item = store.get(record["item_id"])
    results = inspect(ctx, registry, record, tool)
    assert all(not result.is_error and result.images for result in results)
    saved = [json.loads(line) for line in (ctx.ws.memory / "qc_inspections.jsonl").read_text().splitlines()]
    assert len(saved) == len(ctx.state["qc_inspections"]) == 2
    for result, receipt, on_disk in zip(results, ctx.state["qc_inspections"], saved, strict=True):
        assert {key: value for key, value in receipt.items() if key != "response_index"} == result.data
        assert {key: value for key, value in on_disk.items() if key not in {"ts", "actor"}} == result.data
    assert report(ctx, registry).is_error  # Images generated in this response are not yet seen.
    assert store.get_recheck(record["id"])["status"] == "prepared"
    ctx.state["provider_response_index"] = 1
    ctx.state["qc_model"] = "not-the-trusted-record-model"
    accepted = report(ctx, registry)
    assert not accepted.is_error and accepted.data["recorded"]
    completed = QCStore(ctx.state["qc_parent"]).get_recheck(record["id"])
    assert completed["status"] == "completed"
    assert completed["model"] == record["model"] and completed["provider"] == record["provider"]
    assert completed["result"]["evidence"] == [result.data for result in results]
    assert ctx.state["qc_recheck_report"]["evidence"] == completed["result"]["evidence"]
    assert store.get(record["item_id"]) == original_item
    assert not report(ctx, registry).is_error
    journal = ctx.state["qc_parent"].root / ".qc-chat/events.jsonl"
    assert sum(json.loads(line)["type"] == "recheck.completed" for line in journal.read_text().splitlines()) == 1


@pytest.mark.parametrize("failure", ["before_journal", "after_journal"])
def test_persistence_failure_is_not_acknowledged_and_can_be_retried(inspection, monkeypatch, failure):
    ctx, registry, store, record = inspection
    assert all(not result.is_error for result in inspect(ctx, registry, record))
    ctx.state["provider_response_index"] = 1
    append, snapshot = QCStore._append, QCStore._write_snapshot

    def fail_append(self, directory, state, kind, data, actor, model=None):
        if kind == "recheck.completed":
            raise OSError("Synthetic pre-journal failure")
        return append(self, directory, state, kind, data, actor, model)

    def fail_snapshot(self, directory, state):
        if state["rechecks"][record["id"]]["status"] == "completed":
            raise OSError("Synthetic lost completion acknowledgement")
        return snapshot(self, directory, state)

    with monkeypatch.context() as patch:
        if failure == "before_journal":
            patch.setattr(QCStore, "_append", fail_append)
        else:
            patch.setattr(QCStore, "_write_snapshot", fail_snapshot)
        rejected = report(ctx, registry)
    assert rejected.is_error
    assert "qc_recheck_report" not in ctx.state
    expected = "prepared" if failure == "before_journal" else "completed"
    assert store.get_recheck(record["id"])["status"] == expected
    accepted = report(ctx, registry)
    assert not accepted.is_error and accepted.data["recorded"]
    assert store.get_recheck(record["id"])["status"] == "completed"


def test_report_reverifies_pins_before_acknowledging(inspection):
    ctx, registry, store, record = inspection
    assert all(not result.is_error for result in inspect(ctx, registry, record))
    parent = ctx.state["qc_parent"]
    (parent.root / record["after"]["file"]).write_bytes(b"Changed after inspection")
    ctx.state["provider_response_index"] = 1
    result = report(ctx, registry)
    assert result.is_error and "QCPinError" in result.text
    assert "qc_recheck_report" not in ctx.state
    assert store.get_recheck(record["id"])["status"] == "prepared"


def test_provider_receives_no_images_and_cannot_use_failed_encoding_as_evidence(prepared, monkeypatch):
    parent, _, record, cfg = prepared
    calls = []
    responses = iter([
        fixtures.fixtures.reply(blocks=[
            fixtures.tool(side, "qc_frames", {"target": record[side]["target"], "f0": record[side]["f0"],
                                              "f1": record[side]["f1"], "count": 2})
            for side in ("before", "after")
        ], reason="tool_use"),
        fixtures.report_response(), fixtures.fixtures.reply("Unable to inspect the failed images."),
    ])

    def post(provider, body, activity=None):
        calls.append(copy.deepcopy(body))
        return next(responses)

    def fail_encoding(_):
        raise ValueError("Synthetic JPEG conversion failure")

    monkeypatch.setattr(provider_loop, "_post", post)
    monkeypatch.setattr(qc_chat_tools, "jpeg_bytes", fail_encoding)
    completed = run(parent, record["id"], cfg=cfg)["recheck"]
    assert completed["result"]["assessment"] == "inconclusive"
    assert completed["result"]["evidence"] == []
    assert len(calls) == 3
    inspected = calls[1]["messages"][-2:]
    assert all(message["content"][0]["is_error"] for message in inspected)
    assert all(block["type"] == "text" for message in inspected for block in message["content"][0]["content"])
    assert "Synthetic JPEG conversion failure" in json.dumps(inspected)
    assert "Inspect both selected windows" in json.dumps(calls[2])
