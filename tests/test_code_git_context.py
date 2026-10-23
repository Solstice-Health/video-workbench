from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from vwb.code.git_context import GitContext


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["/usr/bin/git", "-C", str(repo), *args], check=True,
                          capture_output=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(repo.parent), "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "Fixture",
                               "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture",
                               "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}).stdout.decode()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def entry():\n    return 'initial'\n")
    (root / "other.py").write_text("def other():\n    return 1\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial fixture")
    return root


def snapshot(directory: Path) -> dict:
    return {str(p.relative_to(directory)): (p.stat().st_size, p.stat().st_mtime_ns,
                                          hashlib.sha256(p.read_bytes()).hexdigest())
            for p in directory.rglob("*") if p.is_file()}


def test_non_git_is_clear_and_does_not_initialize(tmp_path: Path):
    result = GitContext(tmp_path).context({})
    assert result["available"] is False
    assert not (tmp_path / ".git").exists()


def test_does_not_find_host_parent_repository(repo: Path):
    work = repo / "public"
    work.mkdir()
    assert GitContext(work).context({})["available"] is False


def test_live_status_tracks_staged_unstaged_untracked_and_rename(repo: Path):
    git(repo, "mv", "other.py", "renamed.py")
    (repo / "src" / "main.py").write_text("def entry():\n    return 'changed'\n")
    (repo / "new.py").write_text("NEW = 1\n")
    before = snapshot(repo / ".git")
    result = GitContext(repo).context({"active_files": ["src/main.py"]})
    assert result["repository"]["branch"] == "main"
    assert len(result["repository"]["head"]) == 40
    assert result["counts"] == {"staged": 1, "unstaged": 1, "untracked": 1, "protected_omitted": 0}
    rows = {row["path"]: row for row in result["files"]}
    assert rows["renamed.py"]["previous_path"] == "other.py"
    assert rows["renamed.py"]["staged"] is True
    assert rows["src/main.py"]["unstaged"] is True
    assert rows["new.py"]["untracked"] is True
    assert result["active_files"][0]["sha256"] == hashlib.sha256((repo / "src/main.py").read_bytes()).hexdigest()
    assert result["freshness"]["cached"] is False
    assert snapshot(repo / ".git") == before


def test_nested_project_and_scope_only_see_selected_root(repo: Path):
    (repo / "src" / "main.py").write_text("EDIT = True\n")
    (repo / "other.py").write_text("OTHER = False\n")
    work = repo.parent
    result = GitContext(work).context({"root": "project/src"})
    assert result["repository"]["root"] == "project"
    assert result["scope"] == "project/src"
    assert [row["path"] for row in result["files"]] == ["project/src/main.py"]
    result = GitContext(work).context({"root": "project", "path": "src"})
    assert [row["path"] for row in result["files"]] == ["project/src/main.py"]


def test_status_and_diff_are_fresh_after_edits_rename_branch_switch(repo: Path):
    context = GitContext(repo)
    first = context.context({"active_files": ["other.py"]})
    git(repo, "checkout", "-b", "feature")
    git(repo, "mv", "other.py", "feature.py")
    (repo / "feature.py").write_text("FEATURE_BRANCH = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Feature rename")
    feature = context.context({"active_files": ["feature.py", "other.py"]})
    assert first["repository"]["head"] != feature["repository"]["head"]
    assert feature["repository"]["branch"] == "feature"
    assert feature["active_files"][1]["missing"] is True
    git(repo, "checkout", "main")
    final = context.context({"active_files": ["other.py"]})
    assert final["repository"]["head"] == first["repository"]["head"]
    assert final["active_files"][0]["sha256"] == first["active_files"][0]["sha256"]


def test_diff_default_against_head_staged_and_resolved_base(repo: Path):
    context = GitContext(repo)
    (repo / "src" / "main.py").write_text("STAGED = True\n")
    git(repo, "add", "src/main.py")
    (repo / "other.py").write_text("UNSTAGED = True\n")
    all_lines = "\n".join(row["text"] for row in context.diff({})["lines"])
    staged = "\n".join(row["text"] for row in context.diff({"staged": True})["lines"])
    assert "+STAGED = True" in all_lines and "+UNSTAGED = True" in all_lines
    assert "+STAGED = True" in staged and "UNSTAGED" not in staged
    assert context.diff({"base": "refs/heads/main"})["base"] == git(repo, "rev-parse", "HEAD").strip()


def test_deleted_path_filter_remains_valid(repo: Path):
    (repo / "other.py").unlink()
    result = GitContext(repo).diff({"path": "other.py"})
    assert any(row["text"].startswith("-def other") for row in result["lines"])


def test_status_diff_pagination_and_unicode_names(repo: Path):
    for index in range(5):
        (repo / f"new-{index}.py").write_text("VALUE = 1\n")
    first = GitContext(repo).context({"limit": 2})
    second = GitContext(repo).context({"limit": 2, "offset": first["next_offset"]})
    assert len(first["files"]) == len(second["files"]) == 2
    assert first["files"][0] not in second["files"]
    path = repo / "sp ace Ω.py"
    path.write_text("BEFORE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Unicode fixture")
    path.write_text("AFTER = 2\n")
    result = GitContext(repo).diff({"path": path.name, "limit": 3})
    assert len(result["lines"]) == 3 and result["next_offset"] == 3
    rest = GitContext(repo).diff({"path": path.name, "offset": 3})
    assert any("+AFTER = 2" in row["text"] for row in rest["lines"])


def test_sensitive_files_not_exposed_by_all_repository_diff(repo: Path):
    (repo / ".env").write_text("NEVER_SHOW_THIS = old\n")
    (repo / ".secrets").mkdir()
    (repo / ".secrets" / "keys.txt").write_text("SECOND_SECRET = old\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Protected fixture")
    (repo / ".env").write_text("NEVER_SHOW_THIS = new\n")
    (repo / ".secrets" / "keys.txt").write_text("SECOND_SECRET = new\n")
    (repo / "other.py").write_text("PUBLIC = True\n")
    context = GitContext(repo)
    result = context.context({})
    assert result["counts"]["protected_omitted"] == 2
    diff = context.diff({})
    assert diff["protected_omitted"] == 2
    assert "NEVER_SHOW_THIS" not in str(diff)
    assert "SECOND_SECRET" not in str(diff)
    assert "+PUBLIC = True" in str(diff)
    with pytest.raises(ValueError):
        context.diff({"path": ".env"})


@pytest.mark.parametrize("base", ["--help", "HEAD --output=/tmp/pwned", "HEAD..main", "HEAD@{0}",
                                 "$(touch pwned)", "HEAD\nmain", "-c core.pager=bad"])
def test_revision_option_injection_is_refused(repo: Path, base: str):
    with pytest.raises(ValueError):
        GitContext(repo).diff({"base": base})


@pytest.mark.parametrize("root", ["..", "/tmp", "src/../../outside", ".git"])
def test_root_boundary_refused(repo: Path, root: str):
    with pytest.raises(ValueError):
        GitContext(repo).context({"root": root})


def test_git_pointer_cannot_escape_workspace(tmp_path: Path):
    root = tmp_path / "work"
    root.mkdir()
    (root / ".git").write_text("gitdir: ../host-private-git\n")
    with pytest.raises(PermissionError, match="outside"):
        GitContext(root).context({})


def test_common_git_pointer_cannot_escape_workspace(repo: Path):
    (repo / ".git" / "commondir").write_text("../../host-private-git\n")
    with pytest.raises(PermissionError, match="outside"):
        GitContext(repo).context({})


def test_git_worktree_pointer_inside_authorized_workspace_is_supported(repo: Path):
    other = repo.parent / "another-checkout"
    git(repo, "worktree", "add", "-b", "second-checkout", str(other))
    result = GitContext(repo.parent).context({"root": "another-checkout"})
    assert result["available"] is True
    assert result["repository"]["root"] == "another-checkout"
    assert result["repository"]["branch"] == "second-checkout"


def test_metadata_symlinks_alternates_and_config_includes_are_refused(repo: Path):
    (repo / ".git" / "objects" / "linked").symlink_to(repo.parent)
    with pytest.raises(PermissionError, match="symbolic"):
        GitContext(repo).context({})
    (repo / ".git" / "objects" / "linked").unlink()
    (repo / ".git" / "objects" / "info" / "alternates").write_text("/tmp/private-objects\n")
    with pytest.raises(PermissionError, match="alternate"):
        GitContext(repo).context({})
    (repo / ".git" / "objects" / "info" / "alternates").unlink()
    with (repo / ".git" / "config").open("a") as stream:
        stream.write('[include]\n    path = /tmp/host-private-config\n')
    with pytest.raises(PermissionError, match="includes"):
        GitContext(repo).context({})


def test_fifo_git_config_is_rejected_without_blocking(repo: Path):
    config = repo / ".git" / "config"
    config.unlink()
    os.mkfifo(config)
    with pytest.raises(PermissionError, match="regular"):
        GitContext(repo).context({})


def test_worktree_config_includes_and_symlinks_are_rejected(repo: Path):
    git(repo, "config", "extensions.worktreeConfig", "true")
    config = repo / ".git" / "config.worktree"
    config.write_text('[includeIf "gitdir:./"]\n    path = /tmp/host-private-config\n')
    with pytest.raises(PermissionError, match="includes"):
        GitContext(repo).context({})
    config.unlink()
    config.symlink_to(repo / "other.py")
    with pytest.raises(PermissionError, match="symbolic"):
        GitContext(repo).context({})


def test_external_diff_textconv_fsmonitor_are_never_executed(repo: Path):
    script = repo / "untrusted.sh"
    marker = repo / "EXECUTED"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    script.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(script))
    git(repo, "config", "diff.external", str(script))
    git(repo, "config", "diff.malicious.textconv", str(script))
    (repo / ".gitattributes").write_text("*.py diff=malicious\n")
    (repo / "other.py").write_text("CHANGED = True\n")
    context = GitContext(repo)
    assert context.context({})["available"]
    assert "+CHANGED = True" in str(context.diff({}))
    assert not marker.exists()


@pytest.mark.parametrize("config_name", ["config", "config.worktree"])
@pytest.mark.parametrize("driver", ["clean", "process", "smudge"])
@pytest.mark.parametrize("header", ['filter "probe"', "FiLtEr.probe"])
def test_git_filters_are_rejected_before_status_or_diff(repo: Path, config_name: str, driver: str, header: str):
    marker = repo / "FILTER_EXECUTED"
    (repo / ".gitattributes").write_text("other.py filter=probe\n")
    (repo / "other.py").write_text("CHANGED = True\n")
    config = repo / ".git" / config_name
    original = config.read_text() if config.exists() else ""
    # Git accepts a leading UTF-8 BOM; it must not bypass first-section checks.
    config.write_text(f'\ufeff[{header}]\n    {driver} = /usr/bin/touch "{marker}"\n' + original)
    context = GitContext(repo)
    for query in (context.context, context.diff):
        with pytest.raises(PermissionError, match="filter configuration is unsupported"):
            query({})
        assert not marker.exists()


def test_parent_queries_do_not_run_submodule_filters(repo: Path):
    child = repo / "submodule"
    child.mkdir()
    git(child, "init", "-b", "main")
    (child / "tracked.txt").write_text("before\n")
    (child / ".gitattributes").write_text("tracked.txt filter=probe\n")
    git(child, "add", ".")
    git(child, "commit", "-m", "Child fixture")
    git(repo, "add", "submodule")
    git(repo, "commit", "-m", "Embedded Gitlink fixture")
    marker = repo / "SUBMODULE_FILTER_EXECUTED"
    git(child, "config", "filter.probe.clean", f'/usr/bin/touch "{marker}"')
    (child / "tracked.txt").write_text("after changes\n")
    context = GitContext(repo)
    assert context.context({})["available"]
    assert not marker.exists()
    assert context.diff({})["available"]
    assert not marker.exists()


def test_provider_keys_and_git_environment_not_inherited(repo: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-fixture-key")
    monkeypatch.setenv("GIT_DIR", "/tmp/other-git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    assert "OPENAI_API_KEY" not in GitContext._env()
    assert "GIT_CONFIG_COUNT" not in GitContext._env()
    assert GitContext(repo).context({})["available"]


def test_history_is_bounded_and_path_scoped(repo: Path):
    (repo / "other.py").write_text("SECOND = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Second fixture")
    context = GitContext(repo)
    history = context.history({"limit": 1})
    assert history["commits"][0]["subject"] == "Second fixture"
    assert history["next_offset"] == 1
    assert context.history({"limit": 1, "offset": 1})["commits"][0]["subject"] == "Initial fixture"
    assert context.history({"path": "src"})["commits"][0]["subject"] == "Initial fixture"


def test_unborn_repository_is_useful_and_readonly(tmp_path: Path):
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "first.py").write_text("FIRST = True\n")
    context = GitContext(tmp_path)
    assert context.context({})["repository"]["unborn"] is True
    assert context.context({})["files"][0]["path"] == "first.py"
    assert context.diff({})["lines"] == []
    assert context.history({})["commits"] == []


def test_detached_head(repo: Path):
    git(repo, "checkout", "--detach", "HEAD")
    assert GitContext(repo).context({})["repository"]["detached"] is True


def test_long_diff_lines_are_bounded(repo: Path):
    (repo / "other.py").write_text("X" * 90_000 + "\n")
    result = GitContext(repo).diff({"max_bytes": 10_000})
    assert result["shortened_lines"] == 1
    assert len(str(result).encode()) < 12_000


def test_active_files_refuse_symlink_and_sensitive_paths(repo: Path):
    (repo / "shortcut.py").symlink_to(repo / "other.py")
    with pytest.raises(ValueError):
        GitContext(repo).context({"active_files": ["shortcut.py"]})
    with pytest.raises(ValueError):
        GitContext(repo).context({"active_files": [".env"]})


def test_git_context_runner_timeout_and_capture_bounds(repo: Path, monkeypatch):
    context = GitContext(repo)
    repository = context._repository({})
    assert repository is not None
    raw, truncated, code = context._run(repository, ["log", "--format=%H"], cap=4)
    assert len(raw) == 4 and truncated and code == 0
    before = os.environ.get("OPENAI_API_KEY")
    context.context({})
    assert os.environ.get("OPENAI_API_KEY") == before
