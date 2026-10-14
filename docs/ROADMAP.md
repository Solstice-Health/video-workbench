# Roadmap

## Non-QC spec review, 2026-10-14

Compared the supplied *Video Workbench Plain Spec*, version 7, with the current
implementation. The spec is a design proposal, not evidence that a capability is
built. Its named main model and fixed worker roster have been superseded by the
continuing native thread and agent-chosen helpers. QC and judge work is excluded
from this comparison.

Already present: source-video indexing and SQL/search, continuing conversations,
optional helpers, browser and web tools, questions and message queues, compaction,
token tracking, image/video generation, VTracer tracing, asset revisions, canvas
versions/diffs and rendering. These do not need replacing with a new language or
a new harness.

The direct image/video studios expose the existing generation tools to people.
Request-wide Quick Open addresses the sidebar's former limitation to filtering
already-loaded folders. The experimental indexing branch remains unmerged.

The remaining authoring gaps, in recommended order:

1. **Precise context targets.** Add version-pinned asset, shot, region, copy and
   layer references, plus active-canvas selections. Folder/plugin references exist;
   these narrower targets should resolve to small evidence packets using the
   existing registry and source index.
2. **Restore and alternatives.** Canvas commit/get/diff exists, but an output frame
   snapshot is not a workspace checkpoint. Add restore-as-new-version and request
   forks that share immutable media. Preview changes and retain conversation history.
3. **Steering and planning.** Add editable/reorderable queued messages and optional
   enforced read-only planning. Deliver steering at a safe tool boundary only when
   the selected host supports it; existing Send now interrupts and resumes.
4. **Asset replacement.** Add where-used queries, source-master replacement with an
   affected-output preview, immutable rebinding and dirty-span rendering. Current
   asset revisions and cached alpha plates are a foundation, not that whole workflow.
5. **Campaigns and scoped memory.** The request queue is not a durable hundred-request
   campaign engine. Shared asset ownership, cross-request answers, batch state and
   attributable brand/project rules need their own design and workload test.
6. **Open generators.** Add a pinned, sandboxed generator input/output contract,
   registration and nested compositions. Viewing HTML does not make HTML, Blender,
   Lottie or custom Remotion code registered render-layer generators.
7. **Video evidence navigation.** Add source/output tracing, query coverage receipts,
   general observation writes and measured long-source routing. Generic temporal
   subject tracks and shared recovery scheduling remain partial; VTracer, GrabCut,
   first/last-frame Omni conditioning and image-edit candidates already exist.
8. **Recipe and sourcing procedures.** Generic tools cover more than the three
   current recipe files (clip/remake/shorts). Dedicated reskin, resize, remix and
   content-update procedures, and quarantined web-asset intake with license evidence,
   would make those workflows more repeatable.

Cursor's transferable advantages are the integrated search/edit/recovery workflow:
[local exact and regex retrieval](https://cursor.com/docs/agent/tools/search),
[file checkpoints, queue editing and safe-boundary steering](https://cursor.com/docs/agent/overview),
[optional planning](https://cursor.com/docs/agent/plan-mode), and
[scoped rules](https://cursor.com/docs/rules). Its current search documentation
describes local Instant Grep and says it does not store codebase embeddings for
search. That is a reason to measure exact and structured retrieval first, not to
assume an embedding index will improve this video workspace. Product documentation
does not establish a speed or quality advantage on our workloads.

Built in steps. Each step keeps the harness general (no templates, no recipe-specific code paths in the core) and
adds capability through tools, advisors and checks. `docs/TOOLS_CHECKLIST.md` tracks every item.

## Step 1 (this build): the general harness, v0 slice
Edit index, open canvas, renderer, inspection engine, verifier, ledger, broker, swarm with Codex-in-Docker workers,
Clip and Resize recipes as skills, one real test (10 Shorts).

## Step 2: depth in video indexing (the "Write" side of a video harness)
- Every-frame OCR lever; length routing for long sources (windows, coarse-to-fine readings).
- Logo and object detection from library images; SAM tracks; speaker diarisation; music and speech segmentation.
- Embeddings per collection (frames, descriptions, speech) with similarity search beside exact SQL.
- Observation invalidation by media revision (INDEX-STALE) and `trace` / `compare` tools.
- An offline loop that evolves the index's read and pack programs on gold tasks, keeping only held-out wins (D39).

## Step 3: measured workspace retrieval
The work folder holds canvases, drafts, notes and (behind `code.run`) generator code. Plan:
- **Structure first.** Parse canvases into a JSON-path index (layer ids, copy ids, asset refs, keyframe spans, boxes)
  with cross-references both ways: "which layers cite copy X", "what changes on screen if asset Y is swapped".
- **Code symbols.** tree-sitter for TSX, Python and shader code: definitions, references, imports, call graph.
- **Semantics second.** Chunk embeddings aligned to structural units (a layer, a component), exact keys before
  similarity, labelled "nearest" when similarity answers.
- **Incremental and versioned.** Content-hashed units, per-commit snapshots, impact queries before a render.
- **Shared with the edit index.** One query surface over workspace structure and video evidence ("the layer showing
  the 94% statistic and the frames where that statistic is spoken").

These are candidates, not a commitment to an embedding index. The experimental
indexing branch remains unmerged until paired workloads show a navigation or
quality improvement. Keep exact file search available independently of that work.

## Step 4: agent-authored generators behind the sandbox
Open `code.run` inside containers: agents write Remotion components, HTML scenes or Blender scripts; the harness renders
them as alpha plates with pinned images and attests by rendering twice (D37, X20, X21).

## Step 5: the asset lab ladder and the swap-in loop
Built so far: the image-model split and fills (sunburst extract, inpaint, outpaint, create), deterministic mattes, and
keyed motion (Omni with green-screen plates), all measured and flagged. Next: SAM 3.1 trimaps with BiRefNet-HR or
PyMatting for raster subjects, tracing (VTracer, DiffVG) for vector-like art, K candidates per asset with the scorer
(PSNR_Y, CIEDE2000, edge-band) and analysis-by-synthesis refinement, chained Omni clips for first-middle-last motion,
the NEEDS-SOURCE report, swap-in with dirty re-render, campaign resolve (D15-D18, D23).

## Step 6: chat QC and bulk
Comment anchors, rounds and repair levels, standing checks, a Slack-class adapter; Temporal-backed campaigns of ~100
requests with waves and bulk accept (D24, D23).

## Step 7: the sealed judge on the shared engine
Element linking and the edit algebra, the script gate with planted defects, the playbook library with a held-out
slice, and the informed pass (judge spec). The inspection engine in `qc/` is the shared base.
