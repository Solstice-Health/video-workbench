# Repository navigation

Repository tools are shared by every model adapter through `Registry.call`, with
role restrictions, inherited permissions, cancellation, policy and call logging.
They operate on the conversation's `work/` directory. The existing video index
and general media file browser are separate and continue to work as before.

## Setup

Start Docker and run one of:

```sh
./scripts/setup.sh --ui-only --with-code-tools
# Or, after installing the locked Python environment:
uv run python -m vwb.code.runtime install
uv run python -m vwb.code.runtime status
```

The wheel includes the Docker build files. No npm installation on the host is
required. The image pins Node 24.19.0 by manifest digest, Pyright 1.1.414,
typescript-language-server 6.0.2 and TypeScript 6.0.3 with npm integrity hashes.
TypeScript 6 is deliberate: this server uses `tsserver.js`, rather than the
TypeScript 7 native compiler. Installing the image does not start a model turn.
`vwb doctor` reports readiness without making it mandatory for normal chat.

## Agent workflow

1. `repo_context(root="project")` identifies Git branch/HEAD and modified files.
2. `repo_files(root="project", globs=["**/*.py"])` locates likely source files.
3. `repo_search(root="project", pattern="build_index", mode="files")` narrows to
   matching files. Use `content` for lines/context and `count` for counts.
4. `repo_read(path="project/index.py", offset=0, limit=100)` returns source lines
   and its SHA-256. Carry that hash with line-based evidence.
5. `repo_lsp(operation="definition", path="project/caller.py", line=3, column=10,
   expected_sha256="…")` follows a symbol. Positions use **1-based Unicode
   characters**, converted to LSP UTF-16 internally.
6. After editing, read the diff and query diagnostics/references again. A stale
   hash returns an explicit stale result rather than reusing an old position.

Python, JS, JSX, TS and TSX are supported. LSP capabilities include definitions,
references, hover, document/workspace symbols, implementation, callers/callees and
diagnostics where the server advertises support. Unsupported operations return
that status; an empty or partial result is not evidence that an implementation
does not exist. Dependency types require those dependencies in the workspace;
the service never automatically installs them.
Opened LSP files are limited to 1 MiB each. Freshness checks cover at most 2048
eligible source/config files and 32 MiB of new file reads per call. Narrow the
project root when those bounds are reached; the service reports unavailable
rather than silently treating a partial baseline as complete.

`root` is work-relative. Git `path` filters are root-relative; returned file paths
are always work-relative. Git is discovered only within the mount. No repository
is automatically initialized, and imported local folders remain copies. To use
Git history, put an actual repository wholly inside `work/`; a host worktree
whose `.git` points outside this mount is deliberately rejected.
Git filters (including LFS/custom clean/process/smudge configuration) and config
includes are refused because even read operations can execute them. Parent status
and diffs skip submodule changes; explicitly query a child repository to inspect
it under the same validation rules.

An optional `agent_spawn(role="code_explorer")` provides a separate read-only
navigation conversation. It can send messages and delegate within inherited
permissions, but has no shell, file writes, model-generation tools or edit API.
The main agent chooses whether delegation is useful; small lookups need no child.

## Bounds, ignores and evidence

Search respects `.gitignore`, `.ignore` and `.rgignore`, including non-Git copied
projects, and skips dependency/cache noise by default. `include_ignored=true`
requires a named file/subdirectory. Credentials, Git internals and symlinks stay
excluded. Limits include 8 MiB/source file, 30 seconds/query and 8 MiB captured
ripgrep output. Regex uses ripgrep's default engine, not unbounded Python regex.

Results are paged and explicitly mark incomplete scans. `next_offset` continues
a current page; offsets are not immutable snapshots if the workspace changes.
Narrow the scope after an output/scan bound. Never infer absence from a partial
scan. Content-bearing search rows are checked against a current file snapshot
before being labelled with its SHA-256. `repo_read(expected_sha256=...)` and LSP
positions can reject stale evidence.
The host verifies hashes on returned content and fences explicit reads/LSP
against Docker Desktop mount lag. A complete lexical scan describes the observed
container mount; it is not an atomic host-wide absence proof while files change.
The result's `freshness.host` metadata states this scope explicitly. Re-query
after concurrent edits when an empty result matters.

A bounded 32 MiB/256-file content/hash cache uses device/inode/size/mtime/ctime,
not timestamps alone. It avoids rehashing unchanged inspected files, not scanning
all paths. Git status/diffs are checked live with before/after HEAD metadata.
Language servers retain warm state, synchronize all opened files before queries
and invalidate warm caches when host content hashes change, even if container
attributes lag. Unopened dependency/config changes restart the affected server.
Host source/config manifests are compared before and after semantic queries.
Watcher timing and
missing dependency environments can still make semantic results incomplete;
reported freshness is not a transactional snapshot of the whole repository.

## Canvas context

The focused text/code preview contributes a path hint. A selection contributes
line bounds only when its displayed file fingerprint still matches disk. Hints
live outside the agent-writable `work/` mount and are revalidated on use. Hidden
canvas views clear focus. Repository text and hints are task data, not authority.
This is a saved-file preview, not a code editor: there is no unsaved-buffer overlay.

## Isolation and lifecycle

A private host supervisor owns one code container per active conversation. It
keeps LSP processes/hash caches warm across model turns and providers. Its Unix
socket is in a private user-owned directory with mode 0600; only fixed read-only
RPC operations exist. No project executable runs on the host. The container has
a read-only work mount/root filesystem, no network, no credentials, no host
sockets, no Linux capabilities, a non-root user and memory/CPU/PID bounds.
Only the fixed installed server commands are launched. Project language-server
configuration, plugin execution and arbitrary command requests are not supported.

At most four language servers across two project roots are retained. Idle roots
are evicted; the supervisor expires after five idle minutes or an hour at the next
idle boundary. Cancelling an active lookup stops its owned read-only container;
other callers may need to retry and warm state is rebuilt. Busy requests refuse
promptly rather than building an unbounded queue. To stop a specific service:

```sh
uv run python -m vwb.code.runtime stop --workspace runs/REQUEST_ID/work
```

## Evaluation and next decisions

```sh
uv run python scripts/evaluate_code_tools.py --docker --lsp --output reports/code-tools.json
```

This uses known-answer temporary code/Git fixtures and measures cold/warm lookup
latency, exact evidence, pagination, hash freshness, rename/branch changes and
real Python/TS symbol navigation. It does not measure model task quality or imply
Cursor parity. CI runs the same actual-container acceptance separately from the
fast unit/protocol fixtures. No paid model calls or video renders are involved.
The opt-in `VWB_CODE_SERVICE_INTEGRATION=1 pytest -q
tests/test_code_service_integration.py` additionally checks real container
isolation, reuse across host processes, graceful shutdown cleanup and recovery.

The earlier `feat/workspace-indexing` experiment is not merged. Its SQLite FTS,
Merkle hashing and asset graph remain separate. This branch intentionally starts
with exact lexical search, live Git state and LSP semantics. A persistent sparse
index, semantic retrieval or stronger cross-file transaction tracking should be
added only after larger-repository measurements show a specific gap.

Primary design references: [Cursor search tools](https://cursor.com/docs/agent/tools/search),
[fast regex search](https://cursor.com/blog/fast-regex-search),
[Qwen Code LSP](https://github.com/QwenLM/qwen-code/tree/main/packages/core/src/lsp),
[MiniMax code search](https://github.com/MiniMax-AI/minimax-code/tree/main/packages/agent-tools/src/desktop),
[LSP protocol](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/),
[TypeScript server settings](https://github.com/typescript-language-server/typescript-language-server/blob/master/docs/configuration.md).
These are architectural learnings; no reverse-engineered proprietary code is bundled.
