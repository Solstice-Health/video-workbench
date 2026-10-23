from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from vwb.code.paths import PathViolation, WorkspacePaths, is_sensitive_path
from vwb.code.search import SearchEngine

pytestmark = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is required")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("# first\ndef alpha():\n    return 'hello'\n")
    (tmp_path / "src/b.ts").write_text("export const alpha = 'hello';\n")
    (tmp_path / "README.md").write_text("alpha alpha\nhello world\n")
    (tmp_path / ".config").write_text("alpha config\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / ".ignore").write_text("other/\n")
    (tmp_path / ".rgignore").write_text("third/\n")
    for folder in ("ignored", "other", "third", "node_modules"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "hidden.py").write_text("alpha hidden\n")
    return tmp_path


def test_regex_content_hash_and_context(workspace: Path):
    result = SearchEngine(workspace).search({"pattern": "def alpha", "root": "src", "before": 1, "after": 1})
    assert result["counts_complete"]
    assert result["total"] == 1
    row = result["items"][0]
    assert row["path"] == "src/a.py"
    assert row["line"] == 2
    assert row["text"] == "def alpha():"
    assert row["sha256"] == hashlib.sha256((workspace / row["path"]).read_bytes()).hexdigest()
    assert [item["line"] for item in row["context"]] == [1, 3]


def test_hidden_configs_and_default_ignore_files_outside_git(workspace: Path):
    result = SearchEngine(workspace).search({"pattern": "alpha"})
    assert {item["path"] for item in result["items"]} == {"src/a.py", "src/b.ts", "README.md", ".config"}
    assert result["incomplete"] is False


def test_fixed_regex_case_and_type_filters(workspace: Path):
    (workspace / "src/re.py").write_text("A.B\naXb\n")
    engine = SearchEngine(workspace)
    result = engine.search({"pattern": "a.b", "fixed": True, "case_sensitive": False, "filetype": "py"})
    assert [(item["path"], item["text"]) for item in result["items"]] == [("src/re.py", "A.B")]
    result = engine.search({"pattern": "a.b", "case_sensitive": False, "globs": ["src/re.py"]})
    assert result["total"] == 2


def test_modes_pagination_and_occurrence_counts(workspace: Path):
    engine = SearchEngine(workspace)
    first = engine.search({"pattern": "alpha", "mode": "files", "limit": 2})
    second = engine.search({"pattern": "alpha", "mode": "files", "limit": 2, "offset": first["next_offset"]})
    paths = [item["path"] for item in first["items"] + second["items"]]
    assert paths == sorted(paths)
    assert len(paths) == 4 and len(set(paths)) == 4
    counts = engine.search({"pattern": "alpha", "mode": "count", "root": "README.md"})
    assert counts["items"][0]["matching_lines"] == 1
    assert counts["items"][0]["occurrences"] == 2


def test_scoped_include_ignored_and_non_overridable_secrets(workspace: Path):
    engine = SearchEngine(workspace)
    with pytest.raises(ValueError, match="named file or subdirectory"):
        engine.files({"include_ignored": True})
    (workspace / "ignored/.env").write_text("alpha private\n")
    result = engine.search({"pattern": "alpha", "root": "ignored", "include_ignored": True, "globs": ["**/*"]})
    assert [row["path"] for row in result["items"]] == ["ignored/hidden.py"]


@pytest.mark.parametrize("value", [".env", ".env.local", "secrets.json", "api.KEY", ".aws/credentials",
                                    ".git/config", "sub/id_ed25519", "sub/application_default_credentials.json"])
def test_sensitive_path_policy(value: str, workspace: Path):
    assert is_sensitive_path(value)
    with pytest.raises(PathViolation, match="Credentials"):
        WorkspacePaths(workspace).resolve(value)


@pytest.mark.parametrize("value", ["../README.md", "/etc/passwd", "src/../../secret", "src\\a.py", "C:/secret"])
def test_traversal_and_absolute_paths(value: str, workspace: Path):
    with pytest.raises(PathViolation):
        WorkspacePaths(workspace).resolve(value)


def test_symlink_directory_and_file_escape(workspace: Path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("alpha secret\n")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    (workspace / "src/link.py").symlink_to(outside / "secret.py")
    engine = SearchEngine(workspace)
    assert not any("link" in item["path"] for item in engine.files({})["items"])
    for name in ("link/secret.py", "src/link.py"):
        with pytest.raises(PathViolation):
            engine.read({"path": name})


def test_fifo_is_not_opened_as_regular_file(workspace: Path):
    os.mkfifo(workspace / "fifo")
    with pytest.raises(PathViolation, match="regular"):
        WorkspacePaths(workspace).open_file("fifo")


def test_file_list_null_delimited_paths_and_stable_pages(workspace: Path):
    (workspace / "src/weird\nname.py").write_text("odd\n")
    engine = SearchEngine(workspace)
    result = engine.files({"root": "src", "limit": 1})
    seen = result["items"][:]
    while result["next_offset"] is not None:
        result = engine.files({"root": "src", "limit": 1, "offset": result["next_offset"]})
        seen += result["items"]
    assert len(seen) == 3
    assert "src/weird\nname.py" in [row["path"] for row in seen]


def test_scoped_read_sha_staleness_line_paging_and_empty_files(workspace: Path):
    engine = SearchEngine(workspace)
    result = engine.read({"path": "src/a.py", "offset": 1, "limit": 1})
    assert result["items"] == [{"line": 2, "text": "def alpha():"}]
    assert result["next_offset"] == 2
    (workspace / "src/a.py").write_text("changed\n")
    stale = engine.read({"path": "src/a.py", "expected_sha256": result["sha256"]})
    assert stale["stale"] and stale["items"] == []
    (workspace / "empty.txt").touch()
    assert engine.read({"path": "empty.txt"})["total_lines"] == 0


def test_cache_reuses_content_and_invalidates_same_size_rewrites(workspace: Path, monkeypatch):
    engine = SearchEngine(workspace)
    first = engine.read({"path": "src/a.py"})
    original = engine.paths.open_file
    reads = []

    class Tracking:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            reads.append(size)
            return self.stream.read(size)

    monkeypatch.setattr(engine.paths, "open_file", lambda name: Tracking(original(name)))
    assert engine.read({"path": "src/a.py"})["sha256"] == first["sha256"]
    assert reads == []
    file = workspace / "src/a.py"
    before = file.stat()
    file.write_bytes(file.read_bytes().replace(b"hello", b"world"))
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert engine.read({"path": "src/a.py"})["sha256"] != first["sha256"]
    assert reads


def test_host_evidence_evicts_old_cache_despite_unchanged_mount_attributes(workspace: Path, monkeypatch):
    import vwb.code.search as module
    engine = SearchEngine(workspace)
    first = engine.read({"path": "src/a.py"})
    old_signature = engine._cache["src/a.py"][0]
    (workspace / "src/a.py").write_text("fresh host content\n")
    monkeypatch.setattr(module, "signature", lambda info: old_signature)
    # Model the Desktop attribute cache explicitly: stat alone cannot invalidate.
    assert engine.read({"path": "src/a.py"})["sha256"] == first["sha256"]
    actual = hashlib.sha256((workspace / "src/a.py").read_bytes()).hexdigest()
    engine.accept_host_evidence({"host_evidence": [{"path": "src/a.py", "sha256": actual}]})
    assert engine.read({"path": "src/a.py"})["sha256"] == actual
    engine.accept_host_evidence({"host_evidence": [{"path": "src/a.py", "missing": True}]})
    assert "src/a.py" not in engine._cache
    assert engine._cache_bytes == 0


def test_search_refuses_old_matches_with_new_file_hash(workspace: Path, monkeypatch):
    engine = SearchEngine(workspace)
    original = engine._run

    def change_after_search(command, args):
        result = original(command, args)
        (workspace / "src/a.py").write_text("replacement\n")
        return result

    monkeypatch.setattr(engine, "_run", change_after_search)
    result = engine.search({"root": "src/a.py", "pattern": "alpha"})
    assert result["items"] == []
    assert result["incomplete"] and not result["counts_complete"]
    assert result["total"] is None


def test_search_reports_other_line_edits_as_incomplete(workspace: Path, monkeypatch):
    engine = SearchEngine(workspace)
    original = engine._run

    def change_after_search(command, args):
        result = original(command, args)
        with (workspace / "src/a.py").open("a") as stream:
            stream.write("alpha new match\n")
        return result

    monkeypatch.setattr(engine, "_run", change_after_search)
    result = engine.search({"root": "src/a.py", "pattern": "alpha"})
    assert result["items"][0]["text"] == "def alpha():"
    assert result["incomplete"] and result["total"] is None


def test_binary_and_oversized_read(workspace: Path):
    (workspace / "binary").write_bytes(b"alpha\x00hidden")
    engine = SearchEngine(workspace)
    with pytest.raises(ValueError, match="Binary"):
        engine.read({"path": "binary"})
    with (workspace / "large").open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(PathViolation, match="8 MiB"):
        engine.read({"path": "large"})


def test_huge_line_read_output_is_bounded(workspace: Path):
    (workspace / "long").write_text("x" * 1000 + "\nnext\n")
    result = SearchEngine(workspace).read({"path": "long", "max_bytes": 20})
    assert result["items"][0]["text"] == "x" * 20
    assert result["reason"] == "line_exceeds_output_limit"
    assert result["next_offset"] == 1


def test_capture_cap_does_not_claim_complete_counts(workspace: Path, monkeypatch):
    import vwb.code.search as module
    (workspace / "many").write_text("alpha\n" * 1000)
    monkeypatch.setattr(module, "MAX_CAPTURE_BYTES", 4096)
    result = SearchEngine(workspace).search({"root": "many", "pattern": "alpha", "mode": "count"})
    assert result["incomplete"]
    assert result["total"] is None
    assert result["reason"] == "output_limit"
    assert all(item["counts_complete"] is False for item in result["items"])


def test_cancelled_search(workspace: Path):
    event = threading.Event()
    event.set()
    result = SearchEngine(workspace).search({"pattern": "alpha", "cancel_event": event})
    assert result["reason"] == "cancelled"
    assert result["incomplete"]


def test_subprocess_deadline_and_sanitized_environment(workspace: Path):
    engine = SearchEngine(workspace)
    started = time.monotonic()
    _, reason = engine._run(["/bin/sleep", "5"], {"timeout_s": 0.05})
    assert reason == "timeout" and time.monotonic() - started < 1
    output, reason = engine._run([shutil.which("env")], {})
    keys = {line.split(b"=", 1)[0] for line in output.splitlines()}
    assert keys == {b"PATH", b"LANG", b"LC_ALL", b"HOME"}


@pytest.mark.parametrize("args", [{"pattern": "["}, {"pattern": "a", "timeout_s": 31},
                                   {"pattern": "a", "timeout_s": "slow"}, {"pattern": "a", "mode": "bad"},
                                   {"pattern": "a", "limit": 0}, {"pattern": "a", "offset": -1}])
def test_bad_search_arguments(workspace: Path, args: dict):
    with pytest.raises(ValueError):
        SearchEngine(workspace).search(args)


def test_search_newline_path_and_unicode_text(workspace: Path):
    (workspace / "src/line\nbreak.py").write_text("hello\u2028world\n")
    result = SearchEngine(workspace).search({"pattern": "hello", "root": "src/line\nbreak.py"})
    assert result["items"][0]["path"] == "src/line\nbreak.py"
    assert result["items"][0]["text"] == "hello\u2028world"
    assert json.dumps(result)
