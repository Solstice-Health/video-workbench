# Tools checklist (the harness's memory of what exists and what is next)

Every tool and capability the Video Workbench spec calls for, plus what this build added. One line each.
`[x]` built and exercised · `[~]` partial (what is missing is stated) · `[ ]` planned.
Agent-facing tool names are in `code`; run `vwb tools` for the live list with policy-dial modes.
Spec references: D = decision, X = experiment, R/S = sources (see the spec and the judge spec).

## A. Harness runtime and agent hosts
- [x] Versioned design context: `design_context_schema`, `design_context_import`, `design_context_list`, `design_context_resolve`, `design_context_bind`, `design_context_read`, `design_context_copy`, `design_context_asset`, `design_context_check`. Local/project source snapshots, explicit scope/date filtering, protected safety copy, conditional asset constraints and canvas commit checks. Source approval declarations are not certification. Solstice retrieval, human context-management UI and native-project/visual checks remain later increments. See `docs/DESIGN_CONTEXT.md`.
- [x] Canvas target modal: searchable, staged multi-selection with Apply/Cancel, keyboard focus containment and mobile layout. Native After Effects is a distinct project workflow with read-only host readiness; selecting it never enables automation or converts JSON canvas layers automatically.
- [x] Shared code navigation: `repo_files`, `repo_search`, `repo_read`, `repo_context`, `repo_diff`, `repo_history`, `repo_lsp`, `repo_status`. Ignore-aware bounded ripgrep, SHA-256 evidence/cache and stale reads, live read-only Git context, pinned Python/JS/TS/TSX LSP and optional read-only `code_explorer` child. Private warm service across turns; saved canvas-file/line-selection hints. No semantic embeddings, unsaved editor buffer, persistent AST database or Cursor sparse index. See `docs/CODE_INTELLIGENCE.md`.
- [x] Native After Effects: `after_effects_status`, `after_effects_run`, `after_effects_render`, `after_effects_job`; human-enabled macOS JSX, separate projects, durable jobs and owned aerender processes. Native app access is not sandboxed; interrupted scripts remain uncertain until reconciled. Live Adobe validation requires a licensed installation.
- [x] Blender MCP: pinned MCP for Blender 2.2.2 with hash-locked stdio runtime, local add-on setup, `integration_tools` discovery, `integration_call` inspection/viewport imports and separately human-enabled `integration_execute` Python. No automatic app/add-on preference changes or cloud generation tools.
- [x] Job lead host: GPT-6 Astra (max effort) in Codex in Docker, toolkit over MCP with the swarm attached (user decision 2026-10-09); fallbacks: the stateless GPT loop, then Claude Fable 5.1 on the Agent SDK (tool-restricted, in-process MCP) (D05)
- [x] Main agent recovery (B32): a turn counts only on Codex's `turn.completed`; a turn that dies part-way resumes the same thread once; a thread missing from the host's store restarts from the saved state; a host that cannot start hands the turn to the next host at once and keeps its thread; a thread is dropped only after 2 failed turns in a row; fallbacks keep their own threads and are told the turns they missed (SLOT-FALLBACK). All paths run live 2026-10-12 (killed container, simulated Codex and GPT-loop outages, Claude thread resumed, bogus thread id)
- [x] Worker loop for Gemini (function calling, thought signatures kept, native video parts) (D06)
- [x] Worker loop for OpenAI Responses, stateless for Zero Data Retention (`store=false`, encrypted reasoning replayed, no `previous_response_id`)
- [x] Codex worker host in Docker: one container per task, work/ mounted, model through the key-holding proxy, toolkit over MCP/HTTP (user decision 2026-10-09)
- [x] Toolkit over MCP, stdio (`vwb mcp serve`) and streamable HTTP with bearer tokens (control-plane ToolServer)
- [x] Swarm with a hash-chained plaintext mailbox; background dispatch and wait (`swarm_dispatch`, `swarm_wait`) (D07)
- [x] Roles with tool allowlists and inherited permission intersections: job_lead, worker, scene_miner, planner_b, composer, critic (config/policy.yaml)
- [x] Loop guards: notice after 25 calls with no new artifact; budget notice at 150 calls (spec "Leases, caps and loop guards")
- [x] Result bounds at the gateway: images at most 1568 px and 600 KB, at most 8 per result; text over 60k characters saved to `work/.results/` and paged with `file_read`
- [x] Swarm shutdown at the end of a turn: running workers are cancelled between model turns and their containers removed
- [x] FULL wake: a host without the conversation's thread starts from request, plan, notes, outputs, asks (D05)
- [x] LIGHT wake: every chat message resumes the main agent's own thread (Codex `exec resume`, Agent SDK `resume`) with only the new message and `[harness]` notes (B31); verified 2026-10-12: 7 messages on one Codex thread recalled from history alone; the Agent SDK thread resumed live too
- [x] A resumed thread is told what changed in the harness since its last turn: new, changed and removed tools, references to read again, updated instructions (B35)
- [x] Delegation is the main agent's choice: no prompt or recipe requires workers; the prompt says when each role is worth it (B31). Exercised 2026-10-12: a 3 x 30 s remake ran as one 64-minute main-agent turn with no workers (235 tool calls, 3 repair rounds, request r-20261012-102618)
- [x] Supervised agent trees: agent_spawn, agent_list, agent_send, agent_inbox, agent_wait, agent_interrupt, agent_followup; parent/child and peer messaging, saved worker conversations, targeted subtree interruption and activity monitoring. Native Codex interruption stops its container; API calls cancel at boundaries.
- [x] Spawn bounds: depth 3, 6 children per parent, 24 agents total and 6 active by default. At capacity spawning refuses promptly; parents waiting on descendants cannot deadlock a fixed worker pool.
- [x] Peer messages and child results arrive at tool/model boundaries; durable mailbox and public activity events. They convey no additional permissions.
- [ ] Automatic RUN-DEGRADED worker restart after 150 calls / 150k context (call-budget notices exist)
- [x] image_inspect: bounded workspace/asset image crops and zoom with exact display-to-source coordinate mappings
- [x] sandbox_run: temporary/reusable scripts for any provider, Docker execution, shared work/ only, offline default or separately governed public HTTP(S) proxy; 30-minute deadline, resource/output bounds, background jobs and cancellation; no host execution fallback
- [x] sandbox_environment: persistent request-local Python venvs, pip/package or Git HTTPS installs, freeze receipts; loose pip --target installs also supported
- [x] sandbox_status: cross-turn job polling/cancellation and paged logs; four concurrent jobs/request, no automatic replay after supervisor loss
- [x] pdf_info, pdf_read, pdf_search, pdf_render: metadata/bookmarks, bounded text/search, page/crop/zoom images with coordinate mapping, pinned attachment originals and optional local OCR; no semantic table extractor
- [x] attachment_list, attachment_read, attachment_remember: arbitrary durable chat files, pinned original inspection, saved observations and image/reference replay strategy
- [x] artifact_publish, artifact_list, artifact_feedback: versioned arbitrary HTML review tabs, explicit local assets, item/region/time selections and user-submitted chat feedback
- [x] human_ask mode=blocking|non_blocking and human_wait: cancellable waits; waiting-agent replies resume the existing tool without a duplicate chat turn; stale leases expire and unanswered blocking tickets remain open
- [ ] HostAdapter trials for other worker hosts (Gemini CLI, OpenCode, Google ADK, vk-worker) (X01)
- [ ] microVM per request for host-side agents (today: tool-restricted hosts + Docker for Codex) (D02)

## B. Model gateway and slots
- [x] General model access: `model_providers`, live paged `model_catalog` and `model_request` for configured OpenAI, Google, Anthropic and OpenRouter APIs; native payloads, bounded workspace file inputs, generated output files and receipts. The credential-free `vwb_models` helper bridges `sandbox_run(models=true)` through the same Registry. Catalog metadata and account entitlements vary; no arbitrary endpoints or Realtime/WebSocket sessions. See `docs/MODEL_ACCESS.md`.
- [x] Slots named by function with fallbacks (config/slots.yaml) (D27)
- [x] Retries with backoff on transient errors; SLOT-FALLBACK flag when a fallback serves
- [x] Call log with input and output hashes for every model and tool call (ledger/tools.jsonl) (I4)
- [x] Gemini Files API cache with expiry and re-upload from the durable original (R14)
- [x] Model proxy for containers: per-run token in, real key injected, streamed through, logged with model and token usage (I3, I4); refuses image/video/speech endpoints and hosted generative tools, so generation goes through the registered toolkit
- [~] EGRESS flag lists vendors that received source data (purpose table per vendor not yet written)
- [ ] Capability cards with model fingerprints, end-of-life dates, MODEL-DRIFT and MODEL-EOL-NEAR
- [~] Spend ledger: `vwb spend` sums calls and tokens (input, output, reasoning, cached) per vendor and model from the call log, including calls containerised agents make through the proxy; no prices or SPEND-ALARM yet
- [ ] Multi-vendor watcher panel (MiMo, Qwen, Kimi, Gemini) with lineage rules and PANEL-SPLIT / PANEL-WEAK (D09)
- [ ] Open-weight no-egress path (self-hosted Qwen3.8-27B as watch.alt)
- [x] Image slot `img.sunburst` (gpt-image-2.5-sunburst, fallback flare) and video slot `vid.omni` (gemini-omni-1.1-flash, Interactions API) with retries and call logging
- [x] Studio image and Google/Omni model selection uses compatible configurations in `config/slots.yaml`. OpenRouter video selection accepts searched exact paths from its live video catalog, with model-specific controls and pinned provider/model choices. Async provider IDs survive local interruption; generation is never automatically replayed.

## C. Policy, trust and the ledger
- [x] Policy dial per capability: allow, flag, ask, block; local-mode defaults (D04)
- [x] Levers off/shadow/serve (config/levers.yaml) (D31)
- [x] Append-only hash-chained ledger with chain verification; AUDIT-GAP on a break (I1)
- [x] Control-plane signing key; human events (answer, ack, waive, accept) signed; agents cannot mint them (I2)
- [x] Agents never hold vendor keys: hosts get scrubbed environments; containers get a proxy token (I3)
- [~] Release only after a signed human accept plus the verifier's signed manifest (`vwb accept`)
- [ ] Default-deny egress proxy for containers (today: containers have open egress; only the model key is withheld)
- [ ] Red-team bypass test (X03) and nightly canary attacks
- [ ] Hostile-text injection fixtures (on-screen text, web pages) with "an obeyed injection fails the build"

## D. Job intake and chat desk
- [x] Dated task-specific model guidance (reviewed 2026-10-30) reaches `model_request`, `model_providers`, `model_catalog` and shared worker prompts; after four calendar months agents must browse official docs and refresh discovery before relying on it. Explicit model requests and native payloads remain intact. See `docs/MODEL_ACCESS.md`.
- [x] Image/video studio canvas tabs: image dimensions and effort, uploaded or workspace inputs, reference-guided edits and markup, exact painted-mask inpaint, positioned outpaint, local background masks or model-based subject extraction, and Omni text/frame/reference/edit/extend jobs. Local background removal defaults to `matte`, requires no provider key, saves a separate source-sized inverse-alpha mask, and can load that mask with its pinned source into Inpaint. Asynchronous jobs use `Registry.call` with `gen.image`/`gen.video`; request-local history, idempotency, cancellation, parent-loss cleanup and no restart replay. Outputs and receipts are reusable under `work/studio/`; see `docs/UI.md`.
- [x] Shared browser toolkit: `browser_open`, `browser_observe`, `browser_act`, `browser_files`, `browser_dialog`, `browser_close`; native agent-browser 0.39.1, isolated profiles, headed default or headless, policy/role gates and per-call ledger. Full snapshots plus viewport images; stale observation rejection, explicit human ownership, interrupted-operation fencing and no automatic action replay. Real headed/headless acceptance covers forms, canvas coordinates, stable tabs, iframe references, native prompts, uploads/downloads and cleanup; real Astra/Codex turn completed form + screenshot-based canvas click in five browser calls (2026-10-14). See `docs/BROWSER.md` and `scripts/browser_smoke.py`.
- [~] Remote browser: host-configured CDP transport is implemented; hosted session provisioning, provider authentication/profile management, remote file-transfer adapters and billing shutdown have not been implemented or tested. Local client shutdown disconnects rather than destroys the remote browser.
- [x] `vwb chat`: new request from a message (media paths found in the message) or a follow-up turn (D24 v0 desk)
- [x] Any-file chat attachments: picker, local drop and clipboard images, attachment-only messages, queued/saved previews, immutable original downloads and workspace canvas links. Up to 12 files/message and 4 GiB/file; unsupported binaries retained for sandbox parsing.
- [x] Uploaded-image adapters: lazy bounded pixels for Codex, Claude SDK, Responses, Anthropic and OpenRouter; durable reference-only provider histories, model-capability fallback, saved summaries and original retention across compaction. API payload/recovery tests use mocked responses. Real native Codex CLI sent and retained exact image pixels across two completed turns against a local Responses fixture, with the earlier image absent from the resumed container; no paid calls. Claude SDK input contract checked locally.
- [x] `vwb chat -i`: one conversation over many messages in the terminal; Ctrl-C stops the running turn and keeps the conversation open; one turn at a time per request (turn lock refuses a second message) (B31); exercised 2026-10-12
- [x] `job_spec`: the job lead records what it understood plus every assumed default (ASSUMED-DEFAULT)
- [x] Human events from the CLI: `vwb answer`, `vwb ack`, `vwb waive` (BLOCK needs a senior pair), `vwb accept`
- [x] Review page per request (static HTML: videos, triage, flags with ids, questions, source terms, copy)
- [ ] Comment anchors: time namespaces (out:, src:, ref:), resolver to shot/word/text, deterministic echo first (X04)
- [ ] Rounds and repair levels: L0 parametric (no LLM), L1 hot lead, L2 max effort; interdiff cards
- [ ] Chat verbs `/fix`, `/note`, `/declare`, `/confirm`, `/upload`, `/swap`, `/go`, `/alt`, `/revert`, `/review`, `/fork`
- [ ] Standing checks per resolved comment; REGRESSION:comment
- [ ] Slack-class adapter with SSO (v1); click-to-anchor viewer (v2); play beacon for 1x review of regulated spans
- [ ] Self-serve ladder S0-S3 with matured bounds (D25)

## E. Asking a person (human.ask)
- [x] `human_ask`: typed question, 2-4 options, recommended option, kind, lane, scope, deadline (D43)
- [x] Broker: memory-first answers by key and lane, merging of duplicates, ASK-PENDING; only non-blocking reversible choices timeout to the recommended option (ASSUMED-DEFAULT). Blocking questions, grants and sources never default.
- [x] `human_answers`; answers become signed human events and brand memory
- [ ] Card budget (three top cards) ranked by severity and requests unblocked; campaign-level merging
- [ ] Sealed lane for the judge; brief-lane amendments read by both harnesses
- [ ] Rendered option previews (short clips) on cards

## F. The edit index (video indexing, built from scratch)
- [x] Multi-source editing routes caption words/claims, crop dimensions, person-matte faces, QC, verification rules and review sidecars by explicit media refs. Clip/focus/reframe/copy/asset helpers accept `media_id` and reject ambiguous omissions. Video layers follow matching timeline sources; silent spans and different native FPS keep audio on the canvas clock. Bounded regressions include actual two-video rendering/review without paid calls.
- [x] Every attached or explicitly selected workspace video is registered before the lead, with independent immutable snapshots and SHA-256 deduplication. `media_register(path)` registers agent downloads from safe relative work paths through Registry (`media.register`); `index_build(media_id)` separately gates indexing. Folder references remain lazy. Each additional source has its own index/cache; index tools accept `media_id`, and media tools accept `target="source:<ref>"`.
- [x] Source profile: streams, colour tags, keyframe/GOP map, VFR check (`media.probe`)
- [x] Frame-exact decode through ffmpeg with explicit BT.709 (half-frame seek), verified against sequential decode
- [x] Shots (PySceneDetect adaptive) with frame-exact half-open spans
- [x] Speech reader 1: Whisper large-v3-turbo on MLX with word times and a glossary mined from on-screen text
- [x] Speech reader 2: Gemini on the audio only (cannot copy the slides)
- [x] Word-level consensus: hallucination drop, screen-supported replacement, hyphen-split agreement, marked disagreements
- [x] Word boundaries snapped to real pauses in the audio energy
- [x] Sentences; beats (sentences split at commas past 3.5 s)
- [x] OCR reader 1: Apple Vision accurate, language correction off, change-driven sampling (every change, at least 1/s)
- [x] OCR reader 2: RapidOCR (PP-OCR) on full frames matched to tracks by geometry
- [x] Text tracks across time with reader agreement, rotated-text detection, cap heights
- [x] Text roles: title, body, claim, label, footnote, reference, job code, legal, usage terms, ISI, logo text
- [x] Faces, people and attention saliency (Apple Vision) with face tracks
- [x] Layout blocks per shot (flat-background segmentation, recursive inside panels)
- [x] Audio facts: RMS envelope (10 ms), silences, EBU R128 loudness
- [x] Whole-video reading, family 1 (Gemini watches the video): sections, shots, moments, hazards, marks, style
- [x] Whole-video reading, family 2 (GPT-6.1-Sol from transcript, text and contact sheets); disagreements kept (D19)
- [x] Hazards: usage terms, job codes, ISI, on-screen and spoken claims, references (rules) plus reading claims
- [x] Marks: persistent page furniture, brand-name match, largest clean instance (multi-scale template match)
- [x] Observation store (SQLite) with typed tables, open `observations`, and the single `envelope` view (D35)
- [x] Dossier (the Pack an agent reads first)
- [x] Stage caching with cascade invalidation; model readings keyed by what they read
- [~] Invalidation of observations by media revision (INDEX-STALE not yet raised) (D36)
- [x] Third text reader: a vision model reads native-resolution crops of every line the two OCR engines disagree on; two of three settle it (references 92%, footnotes 81% agreement on the test source)
- [x] Index reuse: a request whose media matches a complete index of the same version starts from it (every stage cached)
- [ ] Every-frame OCR lever for sources up to 5 minutes
- [ ] Length routing for long sources: 2 fps OCR, overlapping windows, coarse-to-fine readings
- [~] People: per-frame person mattes (RobustVideoMatting, ONNX on the Neural Engine, 18 fps at 1080p) cached per source span; OWLv2 object detection and SAM product tracks not yet
- [ ] Speaker diarisation, music/speech segmentation, audio fingerprints
- [ ] Frame and description embeddings with per-collection similarity search
- [ ] 60 px tile states, safe-area maps, static-overlay hazard maps
- [ ] Atom tagger for claims and qualifiers with human confirmation of atom sheets

## G. Exploration tools
- [x] `index_overview`, `index_query` (read-only SQL), `index_search`, `index_transcript`, `index_shots`
- [x] `media_frame` (source or any rendered output, crop, index boxes drawn), `media_contact_sheet`
- [x] `watch_ask`: typed video question to Gemini at a chosen window, fps and resolution; stored as a claim
- [x] `focus_find`: what each spoken beat refers to on screen
- [x] File tools on the work folder: `file_list`, `file_read`, `file_write`, `file_edit`, `file_grep`
- [~] `output_compare` (two rendered versions: changed time ranges and the most-changed frames side by side); `trace` from an output interval to its sources is not built
- [ ] Coverage reports on every search (time span, fps, regions covered)

## H. Clip
- [x] `clip_candidates`: every sentence-aligned span scored with reasons; best non-overlapping set by DP
- [x] `clip_refine`: cuts in speech gaps at energy minima, shot-cut snapping, flash-frame guards
- [x] `clip_validate`: BOUNDARY-WORD, ATOM-BROKEN, self-contained openings, duration, overlap
- [x] Three planner families (job lead, Gemini scene-miner, GPT planner B) (D22 lever)
- [~] K-plan funnel 12 → 6 → 3 with ranking (today: candidates plus three independent plans, ranked by the lead)
- [ ] ISI eligibility (ELIGIBLE, RA_PATH, NOT_ELIGIBLE) at the ruled pace (Q8)
- [ ] Route A (stream-copied GOPs, bit-exact) for trims on keyframes; join tests (X02)
- [ ] Seam battery and player matrix (X15)

## I. Compose (resize and any layout, model-driven)
- [x] Canvas schema vwb.canvas/2: open scene graph (video, image, clip, shape, text, captions); keyframed box, crop, opacity, rotation, colours and path draw (D37)
- [x] No house style: text and caption layers must name font, weight, size and colour; no default colours, effects or motion; examples in CANVAS.md are syntax only
- [x] Font library: 16 open-licence families (sans and serif, 300-900, italics) pinned locally with a manifest; `layout_facts` lists them
- [x] Vector paths (`shape: path`, SVG path data, stroke-draw reveal, dashes, gradients at any angle) for rebuilt charts, diagrams, maps and icons
- [x] `shape_kit`: correct geometry for data graphics as paste-ready shape layers (isometric 3D bars that grow in turn, progress rings, pictogram grids, arrows); placement, values and timing stay the agent's
- [x] `chart_read`: measure a bar chart (bars by colour, value axis fitted from OCR'd ticks, legend series, category labels, annotated preview) so unlabelled charts can be recreated in any form; measured values become authored copy with a provenance note (flagged)
- [x] `clip` layers: registry clips (generated, keyed with alpha) on their own clock: start, clip_in, rate, loop or hold
- [x] `canvas_schema`, `layout_facts`, `canvas_new`, `canvas_check`, `canvas_preview`, `canvas_commit`, `canvas_get`, `canvas_diff`, `canvas_list`
- [x] `canvas_renderers`, `canvas_set_renderer`, and `canvas_new(renderer=...)`: capability-checked working-canvas swaps, immutable renderer pins, separate plate caches and per-render attribution. See `docs/ADAPTABLE_CANVAS.md`.
- [x] Copy deck: `copy_add` mints strings from OCR, transcript or authored text (with a provenance note) ; `copy_list` (D21)
- [x] `canvas_snapshot`: a designed frame (all layers, chosen layers, or without text) as an image asset, for Omni transitions between screens and as a sunburst reference
- [x] Remake checks in `canvas_check` and the verifier: source text shown as image pixels below the legibility floor (fine print has its own floor), source pixels in image layers above 1.25x (UPSCALED)
- [x] The remake recipe replaces resize: every screen re-designed natively, text retyped, charts rebuilt, small art traced or recreated, motion and transitions with Omni
- [x] `reframe_solve`: crop keyframes for any box; narration-guided focus; exhaustive window search that avoids cutting text and faces; drift guard
- [x] Composer role (GPT-6.1-Sol in Codex) drafts canvases in parallel; the job lead reviews and commits
- [x] Masks (rect, rounded, circle, ellipse), shadows, borders, blur, tint, dim and rotation on video, image and clip layers; text animations; plain or karaoke captions
- [x] Recipes and prompts rebuilt around recreation: the source is material (typeset, rebuild as vectors, lift, redraw, extend, clean, animate); a source window is one option, not the default
- [~] Tournaments of K finalists (alternatives are returned; no critic-ranked tournament loop yet)
- [~] Harder-shot strategies: rebuild and mask_rebuild are open to agents through the generative tools and vector layers; exclude_retype and rebuild_isi have no dedicated tooling yet (spec Resize)
- [ ] Freeze-frame and retime spans in the time map; transitions between spans
- [ ] Precomps (a layer whose content is another canvas) (Q12)

## J. Render
- [x] Deterministic compositor (numpy/OpenCV): crops, boxes, masks, effects, rotation, images, clips (memory-mapped frame cache), plates in z-order
- [x] Generic Remotion Scene generator for text, captions, shapes and SVG paths; alpha ProRes 4444 plates cached by content hash; pinned font library
- [x] x264 encode (BT.709 tagged, CFR, CRF 17); two-pass EBU R128 audio to -14 LUFS; mux with faststart
- [x] Batch renders: plates in one Remotion batch, composites in parallel
- [x] Plates cover only the frames where their layers show (offset clock); previews render only the frames they show, as cached stills
- [x] Receipts: input hashes, tool versions, settings, output hash
- [~] Attestation by re-rendering sampled frames and comparing per 32 px tile (full render-twice byte check planned) (D20)
- [ ] Segments and dirty-chunk re-render for swap-ins
- [ ] `generator.register` for agent-authored generator code (Remotion TSX, HTML seek capture, shaders, Blender), behind the `code.run` dial
- [ ] HyperFrames/HTML generator, Lottie import, Blender generator with render-watch-revise (X20, X21)
- [ ] Proxy (draft) renders for fast T1 checks

## K. Asset lab and registry
- [x] Registry: immutable revisions, `slot:<name>@<rev>` refs, provenance computed from tool receipts (D10, D15)
- [x] Mark mining; rung 0 (largest clean instance) and rung 1 (temporal median) recovery as badge and keyed variants (`asset_marks`, `asset_recover`)
- [~] NEEDS-SOURCE flags for recovered marks and retyped fonts (no ranked NEEDS-SOURCE report yet)
- [~] Rung 2: `sunburst_extract method=matte` uses a fringe-free difference key or GrabCut in a box; optional `preserve_rgb=true` retains original RGB and existing source alpha, including faint alpha edges. Studio local masks use this option and register full-source inverse-alpha masks with the `matte.background_mask` receipt (segmented). Whole-frame GrabCut seeds retain a background border. People in footage are matted with RobustVideoMatting (`video_matte`, `matte: person` on video layers, restricted to face-anchored regions, MATTE-RISK); SAM 3.1 trimaps and BiRefNet-HR not yet (SAM3-RASTER) (D18)
- [~] Rung 3: image-model split with gpt-image-2.5-sunburst: `sunburst_extract` (model-redrawn cut-out with alpha, optional subject region, SIFT+RANSAC registration to the source, fidelity PSNR, text agreement), `sunburst_inpaint` (masked regeneration; original pixels outside the feathered mask; optional `exact_mask=true` feathers inward and retains every pixel outside the submitted mask; seam and OCR residue measured; best of up to 4), `sunburst_outpaint` (extend to a new size; placed pixels exact apart from a declared blend band) (GEN-RECOVERED); multi-vendor consensus fill not yet (D17)
- [x] `sunburst_create`: new elements with real alpha, guided by up to 16 reference images; `allow_text` defaults false and generated text is detected by OCR (GEN-CREATED, GEN-TEXT). The studio exposes an explicit Allow text control.
- [x] Rung 4: `asset_trace` (VTracer on colour-quantised cut-outs, edge blends merged, fills snapped to the palette) into vector assets for `shape: svg` layers; silhouette IoU and colour error measured (GEN-RECOVERED:trace); no VLM clean-up or DiffVG yet
- [x] Rung 5: `omni_video` (text, first/last frame, reference images, edit, extend; 3-10 s, up to 1080x1920) with green-screen prompting: the key colour is chosen and validated against the element before the clip is paid for, each frame is keyed against its own measured background (colour unmixing, spill suppression), written as ProRes 4444 with alpha, and holes and noise are measured (GEN-RECOVERED:animate, MOTION-APPROX, MATTE-RISK)
- [x] `asset_view`: any asset on a checkerboard or as six clip frames, with provenance, lineage and measurements
- [x] Matte erosion and feathering for keyed clips (`omni_video` `erode`, `feather`); `clip_rekey` re-keys a clip from its green-screen original with new matte settings, no new generation (B34)
- [x] The keyer measures the key only on border pixels near the requested key colour, so subjects that touch or fill the frame edge are not keyed out (B34)
- [x] `effects.sheen`: a light sweep across an image, clip or video layer, inside its own shape (glare, shine), timed in output frames, once or repeating (B34)
- [ ] Rung 6: NEEDS-SOURCE report as a weighted set cover; source hunter
- [ ] Scorer A (PSNR_Y, CIEDE2000 p95, edge-band PSNR, effective resolution); K tries with refinement
- [ ] Swap-in loop: upload, match and bind, where_used, sweep_baked, STALE-DEPENDENT, dirty re-render (X10)
- [ ] Resolve per campaign: clusters, single-flight leases, string votes, resolve board (D23)
- [~] Plate ladder: baked text removal through `sunburst_inpaint` with index text tracks as the mask; no automatic ladder or font identification yet

## L. Inspection engine (shared with the future judge)
- [x] QC chat foundation: separate API conversation/queue and restricted `qc_lead`; version-pinned findings,
  human-sent immutable editor batches, audit filtering, append-only review events, and new-version invalidation.
  `qc_targets`, `qc_view`, `qc_frames`, `qc_items_read`, `qc_item_create` pass through Registry. See `QC_CHAT.md`.
- [x] Explicit fresh-context rechecks with paired version windows, host-recorded sampled coverage,
  private result history and human-owned resolution. `qc_recheck_report` uses the `qc.recheck`
  capability and a dedicated four-tool reviewer role; observations remain unqualified.
- [ ] Qualified automatic rechecks, tracked annotations and a multi-lane QC review board.
- [x] T0 lint: delivery, black spans, frozen share, silence, loudness, channel balance, bars, flash frames
- [x] T1 declared: what every content window keeps, cuts (TEXT-CLIPPED) and drops (TEXT-DROPPED), type size, faces (masks honoured), upscale, camera motion, safe zone
- [x] Cut boundaries against words (BOUNDARY-WORD)
- [x] OCR read-back of every typeset string with two engines (TEXT-MISMATCH) and typeset size
- [x] Attestation: sampled re-render vs delivered pixels per tile (ATTEST-MISMATCH)
- [x] Audio read-back: transcribe the output, compare with the plan (VO-DIVERGE), edge words heard, caption sync (CAPTION-LAG)
- [x] `qc_run`, `qc_peek` for editor QC (notices only) (D40)
- [ ] `qc.pair` (reference-paired crop questions), `qc.window` (short windows at the needed fps), three-family panel
- [ ] `qc.script` with the script gate (known-good, planted defects, harmless variants, blunt inputs) behind `code.run`
- [ ] Public playbook library with exact keys, evidence logs and retirement; defect generators
- [ ] Null-edit noise floor per file; full reconciliation classes (a)-(e) with UNVERIFIABLE-REGION (D11)
- [ ] Element linking and the edit algebra (keep, transform, replace, remove, add); three-way diff (judge spec)
- [ ] EA IRIS flash checks; repeated-footage check

## M. Verifier and delivery
- [x] Flag catalog with code-set severity and bumps; flags derived from findings; ledger replay of state (D13)
- [x] Standing rules: SOURCE-TERMS, REG-RULE (job code), NEEDS-SOURCE (logo, font), FONT-SUBSTITUTED, TEXT-UNVERIFIED table, CHECK-UNQUALIFIED, EGRESS, ASSUMED-DEFAULT
- [x] Provenance rules per image and clip layer from the asset's registry row: GEN-CREATED, GEN-RECOVERED:{split, fill, extend, animate}, MOTION-APPROX, MATTE-RISK, GEN-TEXT, SYNTH-PERSON; the cue sheet and delivery note list every asset with model and prompt
- [x] TEXT-DROPPED counts regulated text carried as typeset copy (by track id or text match), not only text shown in a source window
- [x] A re-verification of the same version clears the flags it no longer finds (flag.cleared in the ledger)
- [x] Triage colour (RED, YELLOW, GREEN); v0 cannot be GREEN while checks are unqualified (D12)
- [x] Cue sheet, delivery note (from records only), signed manifest per version (`deliver_submit`)
- [x] Control-plane guarantee: every committed head version is rendered and verified before the turn ends (D14)
- [~] Contract floor and declared changes (declared intent and layers; floor checks implicit in the verifier) (D28)
- [x] Sidecars per delivered version: `captions.srt` (two-reader transcript, output timing), `description.md` (title and references shown in the span; draft for review), `thumbnail.jpg`; all in the signed manifest
- [ ] Qualification with seeds and strata; canaries for reviewers

## N. Web scouts and sourcing
- [x] `web_search`, `web_fetch`, `web_research`: read-only OpenRouter scouts, explicit search/fetch budgets, source links, provider usage and policy/role checks; no workspace tools in the scout (D38; see `docs/WEB.md`)
- [ ] Interactive headless browser (click/type/login), raw retrieval adapters and quarantined asset downloads
- [ ] `asset.fetch` into quarantine with licence capture; licence dial (CC0 allow, CC BY flag, commercial ask, unknown block)
- [ ] Source ladder A-E; curated per-brand library; SCIENCE-DEPICTION confirmations

## O. Memory and learning
- [x] `project_memory_search`, `project_memory_read` (`project_memory.read`): immutable cited revisions, current-project isolation, BM25 retrieval and compare-and-swap publication.
- [x] Job memory (`memory_note`, `memory_read`) and plan checkpoint (`plan_update`)
- [x] Brand memory of answers (asks/memory.jsonl)
- [ ] Brand preferences store, batch rules ladder, librarian skill proposals with replay and signature (D26, D39)
- [ ] Gold growth from code-checkable tags; offline harness evolution (investigate, then edit; keep held-out wins)
- [ ] Known gaps: retrieval of accepted work, learned ranker, learned critic (D34)

## Integrations
- [x] Direct MCP connection manager: multiple public, bearer-token or browser OAuth Streamable HTTP servers; persistent host-only credentials, automatic discovery and per-server tool selection, independent of Solstice.
- [x] `integration_execute`: explicitly enabled direct MCP tools that may change remote state, gated separately by `integration.execute`; read-only dispatch stays under `integration_call` / `integration.read`.
- [x] `integration_status`, `integration_tools`, `integration_call`: host-owned Solstice OAuth, bounded live catalog and schema-validated read tools through Registry (`integration.read`); persistent locked refresh-token rotation; embedded preview/document imports with provenance.
- [x] Figma direct: independent host-owned OAuth/PKCE and persistent refresh, typed file/node reads and rendered preview imports through the existing `integration.read` Registry tools. Separate plugin and canvas import folder; no Solstice deployment required.
- [x] Source video optional: chat/CLI intake accepts a brief or design reference; `canvas_new(duration_s=...)` creates an authored output timeline with silent audio. Imported raster references get reusable registry asset refs. Preview, render and review work without a video index or fake source.
- [x] Official Figma desktop MCP: separate plugin, explicit connection to the fixed local Figma endpoint, live schema discovery and approved read-tool dispatch through `integration.read`; bounded screenshot/local asset imports with reusable image refs. Uses the Figma desktop session without copying credentials.
- [ ] Hosted official Figma MCP: requires Workbench's own client approval from Figma; REST app credentials do not satisfy this requirement.
- [ ] Production Workbench Auth0 registration and deployed Solstice Figma extension. The Figma via Solstice plugin is unavailable until the gateway exposes it. The separate Figma direct plugin requires its own Figma app registration and user consent. See `docs/INTEGRATIONS.md`.

## P. Bulk engine
- [ ] Temporal durable jobs; campaigns of ~100 requests; waves; resolve board; triage ordering; bulk accept with unlock rules

## Q. Code and file index for the workspace (to beat Cursor)
- [~] Lexical: `file_grep` and `file_list` over the work folder
- [~] Structural queries: `canvas_query` finds layers by time, type, role, id glob, asset, copy id, copy text or region in a working or committed canvas; no persistent cross-canvas index yet
- [ ] Code symbols: tree-sitter for generator code (TSX, Python, shaders), call and import graphs
- [ ] Semantic index: chunk embeddings next to structure; exact-key lookup first, similarity second
- [ ] Incremental by content hash; per-version snapshots; impact queries ("what renders change if this asset changes")

## R. Judge harness (not built; context only)
- [ ] Sealed judge: blind checklist, perception, alignment, edit algebra, checks, script qualification, informed pass, report (judge spec)

## S. Evaluation
- [~] One full real test: 10 Shorts from the pneumonia source (this build)
- [ ] Gold sets, scorecard, experiments X01-X27
