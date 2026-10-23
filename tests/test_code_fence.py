from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from vwb.code.fence import HostFence
from vwb.code.paths import PathViolation


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("from helper import answer\nprint(answer)\n")
    (tmp_path / "helper.py").write_text("answer = 42\n")
    return tmp_path


def request(tool_operation="read", **arguments):
    return {"id": "fixture", "operation": tool_operation, "arguments": {"path": "main.py", **arguments}}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_expected_old_hash_returns_current_host_stale_before_mount(workspace):
    fence = HostFence(workspace)
    old = sha(workspace / "main.py")
    (workspace / "main.py").write_text("changed\n")
    enriched, early = fence.prepare(request(expected_sha256=old))
    assert early["result"]["stale"] is True
    assert early["result"]["sha256"] == sha(workspace / "main.py")
    assert early["result"]["items"] == []


def test_caller_cannot_supply_trusted_evidence(workspace):
    fence = HostFence(workspace)
    original = {**request(), "host_evidence": [{"path": "outside", "sha256": "forged"}],
                "host_manifest": {"forged": True}}
    enriched, early = fence.prepare(original)
    assert early is None
    assert enriched["host_evidence"] == [{"path": "main.py", "sha256": sha(workspace / "main.py")}]
    assert "host_manifest" not in enriched
    assert original["host_evidence"][0]["path"] == "outside"


def test_lsp_baseline_includes_unopened_sources_and_config(workspace):
    (workspace / "pyproject.toml").write_text("[tool.pyright]\n")
    fence = HostFence(workspace)
    enriched, early = fence.prepare(request("lsp", operation="definition"))
    assert early is None
    assert {row["path"] for row in enriched["host_evidence"]} == {"main.py", "helper.py", "pyproject.toml"}
    assert enriched["host_manifest"]["files"] == 3
    assert HostFence(workspace).wait(enriched)


def test_worker_wait_refuses_old_mount_then_accepts_new_bytes(workspace, tmp_path_factory):
    mount = tmp_path_factory.mktemp("mount")
    (mount / "main.py").write_text("old version\n")
    fence = HostFence(workspace)
    enriched, early = fence.prepare(request())
    worker = HostFence(mount)
    assert worker.wait(enriched, timeout_s=0) is False
    (mount / "main.py").write_bytes((workspace / "main.py").read_bytes())
    assert worker.wait(enriched, timeout_s=0.1) is True


def test_warm_worker_rereads_when_attribute_cache_looks_unchanged(workspace, monkeypatch):
    worker = HostFence(workspace)
    digest = sha(workspace / "main.py")
    force_reads = []

    def digest_with_old_attribute_cache(path, *, force=False):
        force_reads.append(force)
        return digest if force else "old" * 21 + "0"

    monkeypatch.setattr(worker, "_digest", digest_with_old_attribute_cache)
    assert worker.wait({"host_evidence": [{"path": "main.py", "sha256": digest}]}, timeout_s=0)
    assert force_reads == [False, True]


def test_postverification_suppresses_old_read_bytes_and_hash(workspace):
    fence = HostFence(workspace)
    enriched, _ = fence.prepare(request())
    old = enriched["host_evidence"][0]["sha256"]
    response = {"id": "fixture", "result": {"path": "main.py", "sha256": old,
                                                "items": [{"line": 1, "text": "old code"}]}}
    (workspace / "main.py").write_text("new code\n")
    result = fence.verify(enriched, response)["result"]
    assert result["stale"] is True and result["items"] == []
    assert "old code" not in str(result)


def test_search_output_hash_checked_even_without_prepare_inputs(workspace):
    fence = HostFence(workspace)
    enriched, _ = fence.prepare({"id": "fixture", "operation": "search", "arguments": {"pattern": "answer"}})
    response = {"id": "fixture", "result": {"items": [{"path": "helper.py", "sha256": "0" * 64, "text": "old"}]}}
    assert fence.verify(enriched, response)["result"]["stale"] is True


def test_unopened_same_size_mtime_preserved_edit_invalidates_lsp(workspace):
    fence = HostFence(workspace)
    enriched, _ = fence.prepare(request("lsp", operation="definition"))
    helper = workspace / "helper.py"
    before = helper.stat()
    helper.write_text("answer = 99\n")
    os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
    response = {"id": "fixture", "result": {"path": "main.py", "sha256": sha(workspace / "main.py"),
                                                "items": [{"name": "answer"}]}}
    result = fence.verify(enriched, response)["result"]
    assert result["stale"] is True and result["items"] == []


def test_new_or_deleted_source_invalidates_language_manifest(workspace):
    fence = HostFence(workspace)
    enriched, _ = fence.prepare(request("lsp", operation="definition"))
    (workspace / "new.py").write_text("new = 1\n")
    result = fence.verify(enriched, {"id": "fixture", "result": {"items": []}})["result"]
    assert result["stale"] is True


def test_hash_budget_failure_is_explicit_unavailable(workspace, monkeypatch):
    import vwb.code.fence as module
    monkeypatch.setattr(module, "MAX_HASH_FILES", 1)
    enriched, early = HostFence(workspace).prepare(request("lsp", operation="definition"))
    assert early["result"]["status"] == "unavailable"
    assert early["result"]["incomplete"] is True


def test_private_or_symlink_paths_never_host_hashed(workspace):
    (workspace / ".env").write_text("private")
    (workspace / "link.py").symlink_to(workspace / ".env")
    for path in (".env", "../secret", "/etc/passwd", "link.py"):
        with pytest.raises(PathViolation):
            HostFence(workspace).prepare(request(path=path))


def test_host_fence_cache_avoids_rereading_unchanged_content(workspace, monkeypatch):
    fence = HostFence(workspace)
    fence.prepare(request())
    original = fence.paths.open_file
    reads = []

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            reads.append(limit)
            return self.stream.read(limit)

    monkeypatch.setattr(fence.paths, "open_file", lambda path: Stream(original(path)))
    fence.prepare(request())
    assert reads == []


def test_missing_active_file_survives_rename_without_breaking_context(workspace):
    fence = HostFence(workspace)
    enriched, early = fence.prepare({"id": "fixture", "operation": "context",
                                     "arguments": {"active_files": ["main.py", "deleted.py", "missing/child.py"]}})
    assert early is None
    assert {row["path"] for row in enriched["host_evidence"] if row.get("missing")} == {"deleted.py", "missing/child.py"}
    assert HostFence(workspace).wait(enriched)
    response = {"id": "fixture", "result": {"active_files": [{"path": "deleted.py", "missing": True}]}}
    assert fence.verify(enriched, response)["result"]["active_files"] == response["result"]["active_files"]
    (workspace / "deleted.py").write_text("appeared = True\n")
    assert fence.verify(enriched, response)["result"]["stale"] is True


def test_empty_search_does_not_claim_host_verified_absence(workspace):
    fence = HostFence(workspace)
    enriched, early = fence.prepare({"id": "fixture", "operation": "search", "arguments": {"pattern": "missing"}})
    result = fence.verify(enriched, {"id": "fixture", "result": {"items": [], "total": 0}})["result"]
    assert result["freshness"]["host"] == {"verified_file_hashes": 0, "atomic_snapshot": False,
                                             "scope": "returned_file_hashes_only", "search_absence_verified": False}
