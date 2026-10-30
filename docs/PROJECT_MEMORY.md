# Project memory and brand-context workflows

Implemented and research checked: **2026-10-30**.

## Use it

1. Open an **Edit** conversation in a project. Create a project or move an existing
   conversation into one using the conversation menu.
2. Put the brand files in that project's workspace, then select one or more
   **@ folder** references in the composer.
3. Choose **Workflows → Build brand context**, or type `/brand-context` at the start
   of the message. Add instructions about the intended audience, market, channel,
   and any conditional asset restrictions. Send the message.
4. The orchestrator snapshots the selected files and assigns a restricted brand
   specialist. The specialist inspects evidence and submits a candidate; the lead
   reviews it and publishes shared memory. A publication receipt is the confirmation
   that memory was saved. A candidate alone is not published memory.
5. Other conversations in the same project discover a compact catalog automatically.
   Agents use `project_memory_search` and `project_memory_read` to retrieve entries,
   citations, gaps and conflicts. Use `design_context_resolve` and the existing
   output-binding tools for applicable requirements and exact source copy.

A raw absolute path in prose does not select a folder. The folder must be available
in the workspace and selected through the existing @ picker. The UI and backend
require project membership and at least one selected folder. Workflows run in Edit;
the QC conversation retains its separate permissions and history.

Rerunning the same brand key and folder set creates a new memory revision. Adding a
different folder set creates another memory unit, preserving earlier units. Keys
include a stable suffix derived from the selected folder paths. Neither operation
silently changes an already-bound output's design-context version. A renamed brand
key or different selection is a separate unit, not an inferred merge.

## Storage and authority

- **Original evidence:** immutable, hashed design-context packs under the project's
  `.design-context/`. Each source retains its file bytes and supported text anchors.
- **Workflow state:** host-owned `.workflows/` records bind selected sources, worker
  identity, candidate, exclusions and publication to a conversation and project.
- **Shared memory:** `.memory/<key>/HEAD.json` points to an immutable revision with
  atomic, cited entries, a summary, gaps, conflicts and provenance. Publication uses
  a compare-and-swap head check so concurrent work cannot silently overwrite a revision.
  A host-generated coverage report preserves excluded and skipped paths/reasons even
  when the model supplies no gaps; search hits report omission and conflict counts.
- **Review copies:** `work/brand-context/workflows/<id>/` contains input manifests,
  the candidate, the final pack manifest and memory receipt. Editing these copies
  does not edit published memory or the canonical candidate.

`memory_note` remains conversation-local scratch memory. Project memory uses current
project membership and never searches another project's store. Moving a conversation
does not copy the former project's shared memory into its destination.

The specialist is an API worker with only workflow inspection/draft, project-memory
read, and agent messaging tools. It cannot run shell commands, fetch URLs, write
arbitrary files, delegate or publish. Normal configured model providers process its
selected source context; this is not an offline extraction feature. All exposed
workflow tools use Registry permissions, capabilities and call logging. Domain
procedure lives in `recipes/brand-context/SKILL.md`.

The host validates file coverage, source identity and hashes, structured schemas,
required-rule citations, project membership and revision conflicts. It does **not**
prove that a model's interpretation is correct or grant approval. Imported text is
task evidence, never executable instructions. The lead must review the candidate.
Memory summaries never replace exact safety text. Required rules are resolved
separately from relevance-ranked memory retrieval, including unknown scope/conflicts.

## Limits and recovery

One workflow accepts up to 512 files, 512 MiB total, 256 MiB per file, 10,000 scanned
entries and 32 path levels. A final pack supports 128 source entries; larger
collections require explicit exclusions or smaller workflows. Text sources must be
at most 1 MiB. Candidates are bounded to 1 MiB. Oversized inputs fail with an
actionable error instead of being silently truncated. Hidden/cache paths, symlinks,
nonregular files and the workflow's own output subtree are skipped and recorded.

Inspection supports paged UTF-8 text, bounded PDF information/read/search/render,
image crops and sampled video frames. Sampling does not cover audio or every moment
of a video. Unsupported or uninspected material must be reported as gaps/exclusions;
PDF/OCR output does not become authoritative exact safety copy automatically.

Stopping a turn keeps its pinned evidence and any submitted candidate. Resume reuses
the exact originating queue job's workflow; a completed candidate need not be
regenerated. It never falls back to an unrelated previous workflow. The original
human brief is retained and paged to the specialist. A failed snapshot must
be corrected and submitted as a new workflow. Failed or interrupted work does not
publish itself. Restarting the server does not automatically run paid extraction.

## Research behind this increment

There is no demonstrated universal best memory system for this workload. The current
choice is immutable evidence plus structured entries, selective retrieval and an
explicit publication step. Lexical BM25 search covers current memory entries, titles,
summaries and tags; it is not a full-source semantic index or a knowledge graph.

| Primary source | What informs this design | Evidence limits |
| --- | --- | --- |
| [LongMemEval](https://arxiv.org/abs/2410.10813), 2024-10-14, revised 2025-03-04 | Evaluate indexing, retrieval and reading separately; test updates, time and abstention. | Conversational QA does not establish brand-rule accuracy. |
| [FlexA-Mem](https://arxiv.org/abs/2610.33274), 2026-10-23 | Preserve immutable records and derive views to fit context budgets; retain exact source strings. | Recent preprint; its results do not establish a universal winner. |
| [EvoMemBench](https://arxiv.org/abs/2605.18421), 2026-05-18, revised 2026-06-15 | Test changing facts and compare against a strong long-context baseline before adding memory complexity. | Benchmark results depend on workload and implementation. |
| [MemoryArena](https://arxiv.org/abs/2602.16313), 2026-02-18, revised 2026-09-17 | Measure whether remembered evidence improves subsequent actions, beyond recall alone. | Does not directly evaluate this brand workflow. |
| [Zep](https://arxiv.org/html/2501.13956v1), 2025-01-20 | Separate source episodes from derived claims and retain temporal provenance. | Vendor preprint; automatic newest-wins resolution is unsuitable for conflicting brand requirements. |
| [MemoryGraft](https://arxiv.org/abs/2512.16962), 2025-12-18 | Treat retrieved experiences as untrusted evidence and restrict the ingestion worker's capabilities. | Demonstrates a poisoning failure mode, not a universal attack rate. |
| [Anthropic context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents), 2025-09-29 | Keep a small discovery catalog and load evidence as needed. | Engineering guidance, not a comparative benchmark. |

The separation between thread-local state and a project namespace also follows the
[LangGraph memory concepts](https://docs.langchain.com/oss/python/concepts/memory)
(living documentation checked 2026-10-30); no LangGraph dependency is introduced.

Validation covers deterministic dispatch, role restrictions, immutable evidence,
citations, coverage, resume, concurrent revisions, project isolation and composer
interaction using synthetic fixtures. These tests do not establish live-model
extraction quality or regulatory compliance. A next evaluation should compare full
eligible context against retrieval on held-out brand briefs, score exact-copy and
scope correctness, conflict handling, source-supported facts, useful abstention,
subsequent design actions, latency and cost. Add embeddings/reranking only after
measuring misses. Human memory management, explicit retirement/forgetting and
cross-pack conflict reconciliation remain subsequent increments.
