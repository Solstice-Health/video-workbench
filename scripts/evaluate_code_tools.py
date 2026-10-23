#!/usr/bin/env python3
"""Controlled retrieval/freshness evaluation. No paid models or video jobs.

Run with the project Python (``uv run python scripts/evaluate_code_tools.py``).
``--docker --lsp`` additionally exercises the actual isolated service and pinned
Python/TypeScript servers. The baseline is the existing file_grep handler, not
Cursor or any proprietary tool. Timings include adapter/service overhead and are
fixture measurements, not a coding-quality benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from vwb.code.git_context import GitContext  # noqa: E402
from vwb.code.search import SearchEngine  # noqa: E402
from vwb.tools.file_tools import file_grep  # noqa: E402


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["/usr/bin/git", "-C", str(root), *args], check=True, capture_output=True,
                            env={"PATH": "/usr/bin:/bin", "HOME": str(root.parent),
                                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                                 "GIT_AUTHOR_NAME": "Controlled fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                                 "GIT_COMMITTER_NAME": "Controlled fixture",
                                 "GIT_COMMITTER_EMAIL": "fixture@example.invalid"})
    return result.stdout.decode()


class Evaluation:
    def __init__(self, root: Path, *, docker: bool):
        self.root, self.docker = root, docker
        self.search, self.git = SearchEngine(root), GitContext(root)
        self.calls: list[dict] = []
        self.checks: list[dict] = []

    def call(self, operation: str, arguments: dict, label: str, baseline: bool = False) -> dict:
        start = time.perf_counter()
        if baseline:
            result = file_grep(SimpleNamespace(ws=SimpleNamespace(work=self.root)), arguments).data
        elif self.docker:
            from vwb.code.runtime import invoke
            result = invoke(self.root, operation, arguments)
        else:
            target = self.git if operation in {"context", "diff", "history"} else self.search
            result = getattr(target, operation)(arguments)
        self.calls.append({"label": label, "operation": operation, "baseline": baseline,
                           "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                           "response_bytes": len(json.dumps(result).encode()),
                           "returned_items": len(result.get("items", result.get("hits", result.get("files", []))))})
        return result

    def check(self, name: str, observed, expected) -> None:
        self.checks.append({"name": name, "passed": observed == expected, "observed": observed, "expected": expected})

    def pages(self, arguments: dict, label: str) -> list[dict]:
        rows = []
        while True:
            result = self.call("search", arguments, label)
            if result.get("incomplete"):
                raise RuntimeError(f"Controlled fixture search incomplete: {result.get('reason')}")
            rows.extend(result["items"])
            if result["next_offset"] is None:
                return rows
            arguments = {**arguments, "offset": result["next_offset"]}


def fixture(root: Path, files: int) -> dict:
    _git(root, "init", "-b", "main")
    source = root / "src"
    source.mkdir()
    for index in range(files):
        (source / f"module_{index:05}.py").write_text(
            f"# Controlled retrieval fixture {index}\n"
            f"def function_{index:05}(value: int) -> int:\n"
            f"    return value + {index}\n"
            f"COMMON_FIXTURE_MARKER = {index}\n")
    needle = f"src/module_{files // 2:05}.py"
    with (root / needle).open("a") as stream:
        stream.write("EXACT_UNIQUE_FIXTURE_NEEDLE = True\n")
    (root / ".gitignore").write_text("ignored/\n")
    (root / "ignored").mkdir()
    (root / "ignored" / "not_source.py").write_text("OUT_OF_SCOPE_FIXTURE_MARKER = True\n")
    (root / ".env").write_text("PROTECTED_FIXTURE_MARKER=not-a-real-key\n")
    (root / "navigation").mkdir()
    (root / "navigation" / "definition.py").write_text("def transform(value: int) -> int:\n    return value + 1\n")
    (root / "navigation" / "caller.py").write_text("from definition import transform\n\nanswer = transform(2)\n")
    (root / "navigation" / "definition.ts").write_text("export function transform(value: number): number { return value + 1; }\n")
    (root / "navigation" / "caller.ts").write_text("import { transform } from './definition';\nconst answer = transform(2);\n")
    # This is source configuration, never a project-provided language server.
    (root / "navigation" / "tsconfig.json").write_text('{"compilerOptions":{"strict":true},"include":["*.ts"]}\n')
    _git(root, "add", "src", "navigation", ".gitignore")
    _git(root, "commit", "-m", "Controlled source fixture")
    return {"needle": needle, "files": files, "head": _git(root, "rev-parse", "HEAD").strip()}


def run(root: Path, files: int, repeat: int, docker: bool, lsp: bool) -> dict:
    expected = fixture(root, files)
    evaluation = Evaluation(root, docker=docker)
    for _ in range(repeat):
        old = evaluation.call("search", {"pattern": "EXACT_UNIQUE_FIXTURE_NEEDLE", "glob": "src/*.py"},
                              "exact_unique", baseline=True)
        new = evaluation.call("search", {"pattern": "EXACT_UNIQUE_FIXTURE_NEEDLE", "root": "src", "fixed": True},
                              "exact_unique")
        evaluation.check("baseline exact unique", [row["path"] for row in old["hits"]], [expected["needle"]])
        evaluation.check("new exact unique", [row["path"] for row in new["items"]], [expected["needle"]])
        regex = evaluation.call("search", {"pattern": r"def function_0000[0-4]\(", "root": "src"}, "regex")
        evaluation.check("regex exact known paths", sorted({row["path"] for row in regex["items"]}),
                         [f"src/module_{index:05}.py" for index in range(min(files, 5))])
    old_many = evaluation.call("search", {"pattern": "COMMON_FIXTURE_MARKER", "glob": "src/*.py"},
                               "frequent_symbol", baseline=True)
    new_many = evaluation.pages({"pattern": "COMMON_FIXTURE_MARKER", "root": "src", "mode": "files", "limit": 100},
                               "frequent_symbol_paginated")
    evaluation.check("new paginated coverage", len({row["path"] for row in new_many}), files)
    evaluation.check("baseline bounded coverage", len(old_many["hits"]), min(files, 200))
    protected = evaluation.call("search", {"pattern": "PROTECTED_FIXTURE_MARKER|OUT_OF_SCOPE_FIXTURE_MARKER"},
                                 "scope_and_credentials")
    evaluation.check("ignored and credential files excluded", protected["items"], [])
    initial = evaluation.call("read", {"path": expected["needle"]}, "freshness_before_edit")
    changed = "AFTER_EDIT_FIXTURE_MARKER = True\n"
    (root / expected["needle"]).write_text(changed)
    host_after_edit = (root / expected["needle"]).stat()
    stale = evaluation.call("read", {"path": expected["needle"], "expected_sha256": initial["sha256"]}, "reject_stale_sha")
    freshness_probe = {"initial_worker_sha256": initial["sha256"],
                       "host_sha256_after_edit": hashlib.sha256(changed.encode()).hexdigest(),
                       "host_bytes_after_edit": host_after_edit.st_size,
                       "host_mtime_ns_after_edit": host_after_edit.st_mtime_ns,
                       "worker_sha256_after_edit": stale.get("sha256"),
                       "worker_bytes_after_edit": stale.get("bytes"), "stale": stale.get("stale")}
    evaluation.check("old hash rejected after edit", stale["stale"], True)
    fresh = evaluation.call("search", {"pattern": "AFTER_EDIT_FIXTURE_MARKER", "fixed": True}, "freshness_after_edit")
    evaluation.check("fresh content hash", fresh["items"][0]["sha256"], hashlib.sha256(changed.encode()).hexdigest())
    renamed = "src/renamed.py"
    _git(root, "mv", expected["needle"], renamed)
    after_rename = evaluation.call("search", {"pattern": "AFTER_EDIT_FIXTURE_MARKER", "fixed": True}, "freshness_after_rename")
    evaluation.check("renamed search location", [row["path"] for row in after_rename["items"]], [renamed])
    status = evaluation.call("context", {}, "git_status_after_rename")
    rename_rows = [row for row in status["files"] if row["path"] == renamed]
    evaluation.check("Git rename retains original path", rename_rows[0].get("previous_path"), expected["needle"])
    diff = evaluation.call("diff", {"path": renamed}, "git_diff_current")
    evaluation.check("diff contains edited content", any("+AFTER_EDIT_FIXTURE_MARKER" in row["text"] for row in diff["lines"]), True)
    _git(root, "checkout", "-b", "fixture-feature")
    _git(root, "add", "src")
    _git(root, "commit", "-m", "Controlled rename and edit")
    feature = evaluation.call("context", {"active_files": [renamed]}, "git_feature_branch")
    evaluation.check("feature branch fresh", feature["repository"]["branch"], "fixture-feature")
    _git(root, "checkout", "main")
    original = evaluation.call("search", {"pattern": "EXACT_UNIQUE_FIXTURE_NEEDLE", "fixed": True}, "freshness_branch_switch")
    main = evaluation.call("context", {"active_files": [expected["needle"]]}, "git_main_branch")
    evaluation.check("branch switch restores known search", [row["path"] for row in original["items"]], [expected["needle"]])
    evaluation.check("branch switch restores HEAD", main["repository"]["head"], expected["head"])
    lsp_status = "not evaluated; pass --docker --lsp to test pinned servers"
    if lsp:
        python = evaluation.call("lsp", {"operation": "definition", "root": "navigation",
                                           "path": "navigation/caller.py", "line": 3, "column": 10}, "python_definition")
        evaluation.check("Python definition resolves imported symbol",
                         sorted({row.get("path") for row in python["items"]}), ["navigation/definition.py"])
        typescript = evaluation.call("lsp", {"operation": "definition", "root": "navigation",
                                               "path": "navigation/caller.ts", "line": 2, "column": 18}, "typescript_definition")
        evaluation.check("TypeScript definition resolves imported symbol",
                         sorted({row.get("path") for row in typescript["items"]}), ["navigation/definition.ts"])
        lsp_status = "Python and TypeScript imported-definition known answers evaluated"
    measurements = {}
    for baseline in (True, False):
        values = [call for call in evaluation.calls if call["baseline"] == baseline and call["label"] == "exact_unique"]
        measurements["existing_file_grep" if baseline else "new_code_search"] = {
            "calls": len(values), "median_ms": round(statistics.median(item["elapsed_ms"] for item in values), 3),
            "response_bytes": sum(item["response_bytes"] for item in values)}
    return {"schema": "vwb.code-evaluation/1", "created_at": datetime.now(UTC).isoformat(),
            "backend": "isolated_docker_service" if docker else "local_readonly_adapters",
            "fixture": {"source_files": files, "git_commits": 2, "model_calls": 0},
            "passed": all(item["passed"] for item in evaluation.checks), "checks": evaluation.checks,
            "measurements": measurements, "calls": evaluation.calls, "lsp": lsp_status,
            "freshness_probe": freshness_probe,
            "limits": ["Controlled synthetic repository, not real coding-task quality or Cursor parity.",
                       "Adapter timings include IPC/startup overhead; filesystem caches are not controlled.",
                       "No semantic retrieval, embedding, model, video job or agent-trajectory benchmark.",
                       "The existing file_grep result cap and new pagination have different output contracts."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="Exercise the actual isolated service.")
    parser.add_argument("--lsp", action="store_true", help="Evaluate pinned Python and TypeScript servers (requires --docker).")
    parser.add_argument("--files", type=int, default=1200)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.lsp and not args.docker:
        parser.error("--lsp requires --docker; there is no host language-server fallback")
    if not 5 <= args.files <= 10_000 or not 1 <= args.repeat <= 20:
        parser.error("--files must be 5..10000 and --repeat must be 1..20")
    with tempfile.TemporaryDirectory(prefix="vwb-code-evaluation-") as directory:
        root = Path(directory).resolve()
        try:
            result = run(root, args.files, args.repeat, args.docker, args.lsp)
        finally:
            if args.docker:
                from vwb.code.runtime import stop
                stop(root)
    output = args.output or REPO / "runs" / ".checks" / f"code-tools-evaluation-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]),
                      "backend": result["backend"], "measurements": result["measurements"],
                      "report": str(output.resolve())}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
