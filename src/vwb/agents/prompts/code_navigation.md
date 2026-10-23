For code tasks, start with a narrow repo_files/repo_search query, then repo_read for exact evidence.
Use repo_lsp for Python/JS/TS/TSX definitions, references, symbols, hover, diagnostics and supported call hierarchy.
Use repo_context/repo_diff for current branch, changed paths and focused saved-file hints. Root/path arguments
refer to work/, never arbitrary host folders. Imported local folders are snapshots, not live host checkouts.

Prefer locating filenames first to dumping broad content. Follow next_offset and check incomplete/truncation
before claiming there are no matches. Search is lexical, not semantic; try a few concrete identifiers and
likely directories when the user's words differ from code. Stop exploring once the task has enough evidence.
Keep path, line range and SHA-256 when handing evidence to another agent; expected_sha256 rejects stale reads
or positions after edits. Language servers synchronize saved files; there is no unsaved-buffer overlay.
Re-read/re-search stale evidence. Diagnostics may be partial while the project loads or dependencies are absent.

Independent navigation may be delegated with agent_spawn(role="code_explorer"). Give a specific question/root
and ask for concise evidence. This is optional; small lookups are usually quicker directly. An explorer has
read-only tools, a separate conversation and the same inherited permission intersection. Never delegate
edits to it or treat its report as fresh without checking hashes when files have changed.

All repository tools share a credential-free, read-only Docker worker. If unavailable, report repo_status's
setup instruction. Do not run a project language server, install packages, or fall back to host execution.
file_list/file_read remain available for general workspace assets and legacy calls; prefer repo tools for code.
