"""Large attachments and installed environments must not explode cheap file tools."""
from pathlib import Path

import pytest

from vwb.tools import file_tools
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "request")
    ws.work.mkdir(parents=True)
    return ToolContext(ws=ws, agent="test", role="job_lead", gateway=None)


def test_read_pages_line_numbers_without_full_read(ctx, monkeypatch):
    path = ctx.ws.work / "lines.txt"
    path.write_text("one\ntwo\nthree\nfour\n")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("unbounded read_text"))
    first = file_tools.file_read(ctx, {"path": "lines.txt", "offset": 1, "limit": 2})
    assert "    2\ttwo" in first.text and "    3\tthree" in first.text
    assert first.data["next_offset"] == 3
    last = file_tools.file_read(ctx, {"path": "lines.txt", "offset": 3})
    assert last.text == "    4\tfour" and last.data["next_offset"] is None


@pytest.mark.parametrize("args", [{"offset": -1}, {"offset": True}, {"offset": 1.5}, {"limit": 0}, {"limit": 2001}])
def test_invalid_paging_rejected(ctx, args):
    assert file_tools.file_read(ctx, {"path": "missing", **args}).is_error


def test_giant_line_is_bounded_and_does_not_claim_safe_line_resume(ctx):
    path = ctx.ws.work / "huge.txt"
    with path.open("wb") as stream:
        stream.write(b"x" * (file_tools.READ_LINE_BYTES * 2) + b"\nsecond\n")
    result = file_tools.file_read(ctx, {"path": "huge.txt"})
    assert result.data["reason"] == "long_line" and result.data["next_offset"] is None
    assert result.data["scanned_bytes"] <= file_tools.READ_LINE_BYTES + 1
    assert "cannot resume inside" in result.text and "second" not in result.text
    assert len(result.text) < file_tools.READ_BUDGET_CHARS + 300


def test_offset_scan_has_hard_limit(ctx, monkeypatch):
    (ctx.ws.work / "many.txt").write_text("abcdefghij\n" * 100)
    monkeypatch.setattr(file_tools, "READ_SCAN_BYTES", 64)
    result = file_tools.file_read(ctx, {"path": "many.txt", "offset": 99})
    assert result.data["reason"] == "scan_limit" and result.data["scanned_bytes"] <= 64
    assert "increasing the line offset does not bypass" in result.text


def test_default_listing_prunes_packages_and_explicit_glob_can_inspect(ctx):
    (ctx.ws.work / "notes.txt").write_text("notes")
    package = ctx.ws.work / ".sandbox" / "envs" / "task" / "package.py"
    package.parent.mkdir(parents=True)
    package.write_text("package")
    default = file_tools.file_list(ctx, {}).data
    assert [f["path"] for f in default["files"]] == ["notes.txt"]
    explicit = file_tools.file_list(ctx, {"glob": ".sandbox/**/*.py"}).data
    assert [f["path"] for f in explicit["files"]] == [".sandbox/envs/task/package.py"]


def test_listing_stops_at_bound_without_materializing_all_paths(ctx, monkeypatch):
    for i in range(20):
        (ctx.ws.work / f"{i}.txt").write_text("hello")
    monkeypatch.setattr(file_tools, "LIST_MAX_FILES", 5)
    result = file_tools.file_list(ctx, {}).data
    assert len(result["files"]) == 5 and result["truncated"]
    assert result["scanned_entries"] == 5


def test_discovery_and_reads_never_follow_links_outside_work(ctx, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("must not read")
    (ctx.ws.work / "link.txt").symlink_to(outside)
    assert file_tools.file_list(ctx, {}).data["files"] == []
    with pytest.raises(PermissionError):
        file_tools.file_read(ctx, {"path": "link.txt"})
    with pytest.raises(ValueError):
        file_tools.file_list(ctx, {"glob": "../**/*"})


def test_glob_semantics_and_grep_package_pruning(ctx):
    (ctx.ws.work / "top.py").write_text("hello")
    nested = ctx.ws.work / "nested"
    nested.mkdir()
    (nested / "deep.py").write_text("hello")
    assert [f["path"] for f in file_tools.file_list(ctx, {"glob": "*.py"}).data["files"]] == ["top.py"]
    assert len(file_tools.file_list(ctx, {"glob": "**/*.py"}).data["files"]) == 2
    assert len(file_tools.file_grep(ctx, {"pattern": "hello", "glob": "*.py"}).data["hits"]) == 2


def test_narrow_glob_starts_at_concrete_prefix(ctx, monkeypatch):
    for i in range(20):
        (ctx.ws.work / f"unrelated-{i}.txt").write_text("unrelated")
    folder = ctx.ws.work / "reports"
    folder.mkdir()
    (folder / "target.txt").write_text("target")
    monkeypatch.setattr(file_tools, "LIST_SCAN_ENTRIES", 3)
    broad = file_tools.file_list(ctx, {}).data
    assert broad["truncated"]
    narrow = file_tools.file_list(ctx, {"glob": "reports/*.txt"}).data
    assert not narrow["truncated"] and narrow["scanned_entries"] == 1
    assert [row["path"] for row in narrow["files"]] == ["reports/target.txt"]
