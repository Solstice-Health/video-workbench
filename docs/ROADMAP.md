# Roadmap

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

## Step 3: a workspace index better than Cursor's
The work folder holds canvases, drafts, notes and (behind `code.run`) generator code. Plan:
- **Structure first.** Parse canvases into a JSON-path index (layer ids, copy ids, asset refs, keyframe spans, boxes)
  with cross-references both ways: "which layers cite copy X", "what changes on screen if asset Y is swapped".
- **Code symbols.** tree-sitter for TSX, Python and shader code: definitions, references, imports, call graph.
- **Semantics second.** Chunk embeddings aligned to structural units (a layer, a component), exact keys before
  similarity, labelled "nearest" when similarity answers.
- **Incremental and versioned.** Content-hashed units, per-commit snapshots, impact queries before a render.
- **Shared with the edit index.** One query surface over workspace structure and video evidence ("the layer showing
  the 94% statistic and the frames where that statistic is spoken").

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
