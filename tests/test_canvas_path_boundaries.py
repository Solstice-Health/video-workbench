"""Typed output tools cannot read private QC records through paths or symlinks."""
from __future__ import annotations

import pytest

from vwb.canvas.compile import load_working, working_path
from vwb.canvas.store import CanvasStore
from vwb.render.pipeline import render_dir
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.util.jsonio import write_json
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    ws = Workspace(tmp_path / "r-paths")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Review"))
    write_json(ws.root / ".qc-chat/items.json", {"private": "QC_AUDIT_SECRET"})
    return ws


def call(ws, tool, **args):
    return build_registry().call(ToolContext(ws, "job_lead", "job_lead", None), tool, args)


@pytest.mark.parametrize("output", ["../work/probe", "../../.qc-chat", "/tmp/leak", "a/b", "a\\b", "..", "", "a" * 129])
def test_output_traversal_rejected_across_entrypoints(ws, output):
    for tool in ("render_info", "canvas_get", "canvas_new"):
        result = call(ws, tool, output=output, version=1, duration_s=2)
        assert result.is_error, (tool, result.text)
        assert "QC_AUDIT_SECRET" not in result.text
    for invoke in (lambda: working_path(ws, output), lambda: render_dir(ws, output, 1),
                   lambda: CanvasStore(ws.canvas).get(output, 1)):
        with pytest.raises(ValueError):
            invoke()


@pytest.mark.parametrize("version", [0, -1, True, "../items", "1", 1.5, 1_000_000_000])
def test_versions_are_strict_integers(ws, version):
    assert call(ws, "render_info", output="short", version=version).is_error
    with pytest.raises(ValueError):
        CanvasStore(ws.canvas).get("short", version)


@pytest.mark.parametrize("relative", ["renders/short/v1/receipt.json", "qc/short/v1/verification.json",
                                    "canvas/short/v1.json", "work/outputs/short/canvas.json"])
def test_record_symlinks_cannot_reveal_private_qc(ws, relative):
    path = ws.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(ws.root / ".qc-chat/items.json")
    if relative.startswith(("renders", "qc")):
        result = call(ws, "render_info", output="short", version=1)
        assert result.is_error
        assert "QC_AUDIT_SECRET" not in result.text
    elif relative.startswith("canvas"):
        with pytest.raises(PermissionError):
            CanvasStore(ws.canvas).get("short", 1)
    else:
        with pytest.raises(PermissionError):
            load_working(ws, "short")
        assert call(ws, "canvas_new", output="short", duration_s=2, overwrite=True).is_error
    assert "QC_AUDIT_SECRET" in (ws.root / ".qc-chat/items.json").read_text()


@pytest.mark.parametrize("root", ["renders", "qc", "canvas", "work/outputs"])
def test_directory_symlinks_cannot_redirect_output_reads(ws, root):
    path = ws.root / root / "short"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(ws.root / ".qc-chat", target_is_directory=True)
    if root in ("renders", "qc"):
        assert call(ws, "render_info", output="short", version=1).is_error
    elif root == "canvas":
        with pytest.raises(PermissionError):
            CanvasStore(ws.canvas).get("short", 1)
    else:
        assert call(ws, "canvas_new", output="short", duration_s=2).is_error


def test_render_info_keeps_legitimate_receipt_and_verification(ws):
    receipt = {"output": "short.v2", "version": 3, "frames": 60}
    write_json(ws.renders / "short.v2/v3/receipt.json", receipt)
    write_json(ws.qc / "short.v2/v3/verification.json", {"triage": "needs_review", "flags": [
        {"severity": "warn", "kind": "copy", "summary": "Check footnote", "status": "open"}]})
    result = call(ws, "render_info", output="short.v2", version=3)
    assert not result.is_error, result.text
    assert result.data == {"receipt": receipt, "triage": "needs_review", "open_flags": ["warn copy: Check footnote"]}
    assert call(ws, "render_info", output="missing").data["receipt"] == {}
