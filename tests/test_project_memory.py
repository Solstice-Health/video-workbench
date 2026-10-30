"""Project isolation, immutable evidence, publication races and bounded retrieval."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from vwb.design_context import store as context_store
from vwb.project_memory import store
from vwb.project_memory.model import MemoryDraft
from vwb.tools.base import Registry, ToolContext
from vwb.tools.project_memory_tools import TOOLS
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import write_json
from vwb.workspace import Workspace
from vwb.workspace.projects import ProjectStore
from vwb.workspace.request import SUBDIRS, Request


def workspace(root, name):
    ws = Workspace(root / name)
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=name, message="Project memory fixture"))
    return ws


@pytest.fixture
def project(tmp_path):
    projects = ProjectStore(tmp_path)
    ws, peer, outsider = [workspace(tmp_path, name) for name in ("r-first", "r-peer", "r-other")]
    shared = projects.create("Shared")
    projects.move(ws, shared["id"])
    projects.move(peer, shared["id"])
    projects.move(outsider, projects.create("Other")["id"])
    return ws, peer, outsider


def import_pack(ws):
    (ws.work / "safety.txt").write_bytes(b"  Exact safety text.\r\nKeep the full text.  ")
    (ws.work / "guide.md").write_text("# Layout\nUse violet color and generous spacing.\n")
    write_json(ws.work / "pack.json", {
        "brand": "Example", "revision": "1", "sources": [
            {"id": "safety", "kind": "safety_information", "path": "safety.txt", "title": "Safety"},
            {"id": "guide", "kind": "guidelines", "path": "guide.md", "title": "Layout guide", "lines": [2, 2]}],
        "rules": [{"id": "exact-copy", "kind": "required_copy", "level": "required", "sources": ["safety"],
                   "description": "Keep all exact safety text"}]})
    return context_store.import_pack(ws, "pack.json")["pack"]


def draft(key="brand"):
    return {"key": key, "title": "Brand guidance", "summary": "A discovery summary, not safety copy.",
            "entries": [
                {"id": "spacing", "kind": "guidance", "text": "  Use generous spacing.\r\nViolet palette.  ",
                 "sources": ["guide"], "tags": ["layout"]},
                {"id": "warning", "kind": "safety", "text": "Read the complete safety source before editing.",
                 "sources": ["safety"], "tags": []}],
            "gaps": ["Audience has not been supplied."], "conflicts": []}


def publish(ws, pack, value=None, expected=None):
    return store.publish(ws, value or draft(), pack=pack, workflow="wf-selected-folder", expected_revision=expected)


def revision_file(ws, row):
    return ws.project_root / ".memory" / row["key"] / "revisions" / (row["revision"] + ".json")


def test_same_project_conversations_share_exact_records_and_source_provenance(project):
    ws, peer, _ = project
    pack = import_pack(ws)
    value = draft()
    row = publish(ws, pack, value)
    assert store.read(peer, "brand") == row
    assert store.read(peer, "brand", row["revision"]) == row
    assert {k: row[k] for k in value} == value
    assert row["created_at"] and row["created_by"] == ws.id
    assert row["sha256"] == sha256_json({k: v for k, v in row.items() if k != "sha256"})
    assert row["provenance"]["project"] == ws.project_root.name
    source = next(s for s in row["provenance"]["sources"] if s["id"] == "guide")
    assert source["lines"] == [2, 2]
    assert source["sha256"] == hashlib.sha256((ws.work / "guide.md").read_bytes()).hexdigest()
    assert source["text_sha256"] == hashlib.sha256(b"Use violet color and generous spacing.").hexdigest()
    assert store.heads(peer) == [{k: row[k] for k in ("key", "revision", "title", "pack", "workflow")}]
    assert not (ws.root / ".memory").exists()
    assert not (peer.root / ".memory").exists()


def test_cross_project_and_private_pack_exclusion(project, tmp_path):
    ws, _, outsider = project
    pack = import_pack(ws)
    row = publish(ws, pack)
    assert store.heads(outsider) == []
    assert store.search(outsider)["hits"] == []
    with pytest.raises(FileNotFoundError):
        store.read(outsider, "brand", row["revision"])
    with pytest.raises(FileNotFoundError):
        publish(outsider, pack)
    assert not (outsider.project_root / ".memory").exists()
    # Joining a project does not promote the old conversation's private packs.
    private = workspace(tmp_path, "r-private")
    private_pack = import_pack(private)
    projects = ProjectStore(tmp_path)
    projects.move(private, projects.create("Fresh")["id"])
    assert context_store.load(private, private_pack)  # Existing design-context fallback is deliberate.
    with pytest.raises(FileNotFoundError):
        publish(private, private_pack)
    assert not (private.project_root / ".memory").exists()
    # Neither implicit current reads nor explicit history survive moving projects.
    projects.move(ws, outsider.project_root.name)
    with pytest.raises(FileNotFoundError):
        store.read(ws, "brand", row["revision"])


def test_no_project_or_empty_reads_never_create_storage(project, tmp_path):
    ws, _, _ = project
    private = workspace(tmp_path, "r-alone")
    assert store.prompt(private) == ""
    for operation in (lambda: store.heads(private), lambda: store.search(private),
                      lambda: store.read(private, "brand"), lambda: publish(private, "a" * 64)):
        with pytest.raises(ValueError, match="current project"):
            operation()
    assert store.heads(ws) == [] and store.search(ws)["total"] == 0 and store.prompt(ws) == ""
    with pytest.raises(FileNotFoundError):
        store.read(ws, "absent")
    for root in (private.root, ws.root, ws.project_root):
        assert not (root / ".memory").exists()
        assert not (root / ".design-context").exists()


def test_cas_idempotency_and_history_never_overwrite_revisions(project):
    ws, peer, _ = project
    pack = import_pack(ws)
    first = publish(ws, pack)
    original = revision_file(ws, first).read_bytes()
    assert publish(peer, pack) == first
    changed = draft()
    changed["entries"][0]["text"] = "A new chart layout."
    with pytest.raises(store.MemoryConflict):
        publish(peer, pack, changed)
    second = publish(peer, pack, changed, first["revision"])
    assert second["revision"] != first["revision"]
    assert publish(ws, pack, changed, first["revision"]) == second  # Retry after successful publication.
    with pytest.raises(store.MemoryConflict):
        publish(ws, pack, draft(), first["revision"])
    assert store.read(ws, "brand") == second
    assert store.read(ws, "brand", first["revision"]) == first
    assert revision_file(ws, first).read_bytes() == original
    # Explicitly restoring history reuses its original creation metadata and bytes.
    assert publish(ws, pack, draft(), second["revision"]) == first
    assert revision_file(ws, first).read_bytes() == original
    assert store.read(ws, "brand", second["revision"]) == second


def test_concurrent_writers_compare_and_swap_one_head(project):
    ws, peer, _ = project
    pack = import_pack(ws)
    first = publish(ws, pack)
    gate = Barrier(2)

    def writer(index):
        value = draft()
        value["summary"] = f"Concurrent writer {index}"
        gate.wait(timeout=5)
        try:
            return publish((ws, peer)[index], pack, value, first["revision"])
        except store.MemoryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        rows = list(executor.map(writer, range(2)))
    winner = [row for row in rows if row is not None]
    assert len(winner) == 1
    assert store.read(ws, "brand") == winner[0]
    assert store.read(ws, "brand", first["revision"]) == first


def test_interrupted_head_publication_leaves_old_head_and_retry_reuses_snapshot(project, monkeypatch):
    ws, _, _ = project
    pack = import_pack(ws)
    first = publish(ws, pack)
    value = draft()
    value["summary"] = "Changed summary"
    original_atomic = store._atomic

    def fail_head(directory, name, record, **kwargs):
        if name == "HEAD.json":
            raise OSError("Simulated interruption before atomic head replacement")
        return original_atomic(directory, name, record, **kwargs)

    monkeypatch.setattr(store, "_atomic", fail_head)
    with pytest.raises(OSError, match="Simulated"):
        publish(ws, pack, value, first["revision"])
    assert store.read(ws, "brand") == first
    paths = list(revision_file(ws, first).parent.glob("*.json"))
    assert len(paths) == 2
    pending = next(path for path in paths if path != revision_file(ws, first))
    preserved = pending.read_bytes()
    monkeypatch.setattr(store, "_atomic", original_atomic)
    assert publish(ws, pack, value, first["revision"])["revision"] == pending.stem
    assert pending.read_bytes() == preserved


@pytest.mark.parametrize("field,value", [
    ("key", "../escape"), ("key", "Upper_Case"), ("key", "x" * 65), ("key", "a\n"),
    ("title", "\t\n "), ("title", "x" * 201), ("summary", "x" * 4001), ("summary", 3),
    ("summary", "null\0byte"), ("entries", ()), ("entries", [draft()["entries"][0]] * 101),
    ("gaps", [" "]), ("conflicts", ["x"] * 33), ("unrecognized", "value"),
])
def test_draft_is_strict_bounded_and_rejects_blank_text(field, value):
    raw = draft()
    raw[field] = value
    with pytest.raises(ValidationError):
        MemoryDraft.model_validate(raw)


@pytest.mark.parametrize("field,value", [
    ("id", "../escape"), ("kind", "approval"), ("text", "\n\t"), ("text", "x" * 4001),
    ("sources", []), ("sources", ["guide"] * 129), ("sources", ["guide", "guide"]),
    ("sources", [123]), ("sources", [" "]), ("tags", ["x"] * 33), ("tags", ["x" * 101]),
    ("tags", [" "]), ("unknown", True),
])
def test_entries_have_strict_types_and_bounds(field, value):
    raw = draft()
    raw["entries"][0][field] = value
    with pytest.raises(ValidationError):
        MemoryDraft.model_validate(raw)


def test_unique_entry_ids_and_optional_bounded_lists():
    raw = draft()
    raw["entries"].append(copy.deepcopy(raw["entries"][0]))
    with pytest.raises(ValidationError, match="unique"):
        MemoryDraft.model_validate(raw)
    raw = draft()
    del raw["gaps"], raw["conflicts"], raw["entries"][0]["tags"]
    assert MemoryDraft.model_validate(raw).gaps == []


def test_bad_citations_do_not_create_storage(project):
    ws, _, _ = project
    pack = import_pack(ws)
    raw = draft()
    raw["entries"][0]["sources"] = ["missing"]
    with pytest.raises(ValueError, match="unknown pack source"):
        publish(ws, pack, raw)
    assert not (ws.project_root / ".memory").exists()


@pytest.mark.parametrize("target", ["manifest.json", "source-guide.md", "text-guide.txt", "source-safety.txt"])
def test_pack_tampering_blocks_publication_and_reads(project, target):
    ws, _, _ = project
    pack = import_pack(ws)
    row = publish(ws, pack)
    path = ws.project_root / ".design-context" / pack / target
    if target == "manifest.json":
        record = json.loads(path.read_text())
        record["brand"] = "Tampered"
        write_json(path, record)
    else:
        path.write_bytes(b"tampered")
    for operation in (lambda: publish(ws, pack), lambda: store.read(ws, "brand"),
                      lambda: store.search(ws), lambda: store.read(ws, "brand", row["revision"])):
        with pytest.raises(ValueError, match="integrity|digest"):
            operation()


def test_uncited_source_integrity_checked_before_publication(project):
    ws, _, _ = project
    pack = import_pack(ws)
    (ws.project_root / ".design-context" / pack / "source-safety.txt").write_text("corrupt")
    value = draft()
    value["entries"] = value["entries"][:1]
    with pytest.raises(ValueError, match="integrity"):
        publish(ws, pack, value)
    assert not (ws.project_root / ".memory").exists()


@pytest.mark.parametrize("target", ["head", "revision", "revision-with-rehashed-envelope"])
def test_memory_tampering_is_detected(project, target):
    ws, _, _ = project
    row = publish(ws, import_pack(ws))
    path = (ws.project_root / ".memory" / "brand" / "HEAD.json") if target == "head" else revision_file(ws, row)
    changed = json.loads(path.read_text())
    if target == "head":
        changed["revision"] = "b" * 64
    else:
        changed["summary"] = "Tampered"
    if target == "revision-with-rehashed-envelope":
        changed["sha256"] = sha256_json({k: v for k, v in changed.items() if k != "sha256"})
    write_json(path, changed)
    with pytest.raises(ValueError, match="integrity"):
        store.read(ws, "brand")
    with pytest.raises(ValueError, match="integrity"):
        store.search(ws)


@pytest.mark.parametrize("target", ["memory", "key", "head", "revisions", "revision", "lock", "packs", "pack", "source", "text", "manifest"])
def test_symlinks_rejected_for_reads_and_publication(project, tmp_path, target):
    ws, _, _ = project
    pack = import_pack(ws)
    row = publish(ws, pack)
    memory = ws.project_root / ".memory"
    packs = ws.project_root / ".design-context"
    paths = {"memory": memory, "key": memory / "brand", "head": memory / "brand" / "HEAD.json",
             "revisions": memory / "brand" / "revisions", "revision": revision_file(ws, row),
             "lock": memory / "brand" / ".lock", "packs": packs, "pack": packs / pack,
             "source": packs / pack / "source-guide.md", "text": packs / pack / "text-guide.txt",
             "manifest": packs / pack / "manifest.json"}
    path, moved = paths[target], tmp_path / "linked-content"
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises((OSError, ValueError)):
        publish(ws, pack)
    if target != "lock":  # Readers never open/create a lock file.
        for operation in (lambda: store.read(ws, "brand"), lambda: store.heads(ws), lambda: store.search(ws)):
            with pytest.raises((OSError, ValueError)):
                operation()


def test_empty_storage_symlink_cannot_leak_another_project(project):
    ws, _, outsider = project
    publish(ws, import_pack(ws))
    (outsider.project_root / ".memory").symlink_to(ws.project_root / ".memory", target_is_directory=True)
    with pytest.raises(OSError):
        store.read(outsider, "brand")
    with pytest.raises(OSError):
        store.prompt(outsider)


def test_copied_record_cannot_impersonate_another_project(project):
    ws, _, outsider = project
    pack = import_pack(ws)
    original = publish(ws, pack)
    # Both projects can independently import the same pack, but memory provenance is scoped.
    assert import_pack(outsider) == pack
    foreign = publish(outsider, pack)
    revision_file(outsider, foreign).unlink()
    path = revision_file(outsider, original)
    path.write_bytes(revision_file(ws, original).read_bytes())
    with pytest.raises(ValueError, match="provenance"):
        store.read(outsider, "brand", original["revision"])


@pytest.mark.parametrize("target", ["head", "source"])
def test_nonregular_records_fail_without_blocking(project, target):
    ws, _, _ = project
    pack = import_pack(ws)
    publish(ws, pack)
    path = (ws.project_root / ".memory" / "brand" / "HEAD.json" if target == "head" else
            ws.project_root / ".design-context" / pack / "source-guide.md")
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular files"):
        store.read(ws, "brand")


def test_search_is_ranked_paginated_and_only_uses_current_heads(project):
    ws, peer, _ = project
    pack = import_pack(ws)
    value = draft()
    value["entries"] = [{"id": f"entry-{index:03}", "kind": "fact", "text": f"Violet color reference {index}.",
                         "sources": ["guide"], "tags": []} for index in range(23)]
    first = publish(ws, pack, value)
    initial = store.search(peer, "violet")
    assert len(initial["hits"]) == 10 and initial["next_offset"] == 10 and initial["total"] == 23
    second = store.search(ws, "violet", offset=10)
    last = store.search(ws, "violet", offset=20)
    hits = initial["hits"] + second["hits"] + last["hits"]
    assert last["next_offset"] is None and len({h["entry"]["id"] for h in hits}) == 23
    assert all(h["pack"] == pack and h["sources"][0]["id"] == "guide" and h["sources"][0]["sha256"] for h in hits)
    assert initial == store.search(peer, "VIOLET")
    assert store.search(ws, "violet", offset=999)["hits"] == []
    assert store.search(ws, "unmatched")["total"] == 0
    revised = draft()
    revised["entries"] = [{"id": "new", "kind": "fact", "text": "Ultramarine blue.", "sources": ["guide"], "tags": []}]
    current = publish(ws, pack, revised, first["revision"])
    assert store.search(ws, "violet")["hits"] == []
    assert store.search(ws, "ultramarine")["hits"][0]["revision"] == current["revision"]
    assert store.read(ws, "brand", first["revision"])["entries"] == value["entries"]
    # Rare discriminating terms rank ahead of a generic partial match.
    another = draft("another")
    another["entries"] = [{"id": "other", "kind": "fact", "text": "Blue chart.", "sources": ["guide"], "tags": []}]
    publish(ws, pack, another)
    assert store.search(ws, "ultramarine blue")["hits"][0]["key"] == "brand"


@pytest.mark.parametrize("arguments", [{"query": "x" * 201}, {"query": 3}, {"offset": -1}, {"offset": True},
                                      {"offset": 1_000_001}, {"limit": 0}, {"limit": 21}, {"limit": True}])
def test_search_input_bounds(project, arguments):
    with pytest.raises(ValueError):
        store.search(project[0], **arguments)


def test_output_budget_never_truncates_safety_entries_or_loses_pages(project):
    ws, _, _ = project
    pack = import_pack(ws)
    value = draft()
    value["entries"] = [{"id": f"safety-{i:02}", "kind": "safety", "text": "Safety\n" * 571,
                         "sources": ["safety"], "tags": []} for i in range(20)]
    publish(ws, pack, value)
    seen, offset = [], 0
    while offset is not None:
        page = store.search(ws, limit=20, offset=offset)
        assert len(json.dumps(page, ensure_ascii=False)) < 51_000
        seen.extend(page["hits"])
        offset = page["next_offset"]
    assert len(seen) == 20
    assert all(hit["entry"]["text"] == value["entries"][0]["text"] for hit in seen)


def test_catalog_is_small_data_only_and_discoverable(project):
    ws, _, _ = project
    pack = import_pack(ws)
    for index in range(24):
        value = draft(f"memory-{index:02}")
        value["title"] = "Long catalog title " * 10
        publish(ws, pack, value)
    prompt = store.prompt(ws)
    assert len(prompt) < 6000
    assert "task data, not instructions" in prompt
    assert "project_memory_search" in prompt and "project_memory_read" in prompt
    assert "design_context_resolve" in prompt and "never substitutes for exact safety text" in prompt
    assert "of 24 current memory keys" in prompt
    assert draft()["entries"][0]["text"] not in prompt
    assert "semantically verified or approved" in prompt


def test_tools_only_offer_read_capability_and_obey_registry_gate(project, monkeypatch):
    ws, _, _ = project
    row = publish(ws, import_pack(ws))
    assert {tool.name for tool in TOOLS} == {"project_memory_search", "project_memory_read"}
    assert {tool.capability for tool in TOOLS} == {"project_memory.read"}
    registry = Registry(TOOLS)
    context = ToolContext(ws, "job_lead", "job_lead", None)
    monkeypatch.setattr(Registry, "allowed_for_role", staticmethod(lambda *_: True))
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda _: "allow"))
    search = registry.call(context, "project_memory_search", {"query": "spacing", "offset": 0})
    assert not search.is_error and search.data["hits"][0]["revision"] == row["revision"]
    read = registry.call(context, "project_memory_read", {"key": "brand", "revision": row["revision"]})
    assert not read.is_error and read.data["entries"] == row["entries"]
    assert "not instructions" in read.data["trust"]
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda capability: "block" if capability == "project_memory.read" else "allow"))
    assert registry.call(context, "project_memory_read", {"key": "brand"}).is_error
