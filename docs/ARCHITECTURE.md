# Architecture

Video Workbench is a chat-based video editor built as a general agent harness (spec D30). A person chats with one
main agent (the job lead) in a continuing conversation; the main agent plans and builds each output itself as an
editable composition file, and spins up workers only when it chooses; the control plane renders, measures and
checks; a person accepts. This build is the v0 slice for Clip and Resize, plus open-ended composition.

```
 person ── vwb chat ──▶ CONTROL PLANE (the vwb process; holds keys, ledger, verdicts)
                         │  intake ─▶ edit index ─▶ main agent turn ─▶ ensure review ─▶ review page
                         │  tool gateway: role allowlist · policy dial · call log (hashes) · loop guards
                         │  model gateway: slots · retries · fallbacks · proxy for containers
                         │  verifier: checks ─▶ flags (code-set severity) ─▶ triage ─▶ signed manifest
                         │
 AGENT PLANE             ▼
   main agent    GPT-6 Astra (max) in Codex, its own Docker container; one Codex thread per request,
   (job lead)    resumed on every message; the swarm on its MCP server
                 (fallbacks: the stateless GPT loop, then Claude Fable 5.1 through the direct Anthropic API)
 optional workers, dispatched only when the main agent chooses:
   scene_miner   Gemini 3.8 Flash, harness loop, sees the source video natively
   planner_b ┐
   composer  ├── GPT-6.1-Sol (max) in Codex, one Docker container per task,
   critic    ┘   work/ mounted, model via proxy token, toolkit via MCP/HTTP

 GENERATIVE TOOLS (called by agents, run by the control plane)
   sunburst      gpt-image-2.5-sunburst: create (alpha), extract, inpaint, outpaint; source pixels outside a
                 mask are restored by code; seams, fidelity, text and faces measured
   omni          gemini-omni-1.1-flash: text, first/last frame, reference, edit, extend; green-screen
                 plates keyed back to alpha clips
```

The browser can select a different main-agent model for future submissions. GPT
selections use the Codex Docker host; Anthropic and OpenRouter selections use
persistent host-side API tool loops. Each model keeps separate native history,
and a switch supplies the full saved public conversation or all missed turns.
Queued jobs freeze their model, context budget, effort and speed settings.
Provider credentials remain on the control plane. See [model controls](MODELS.md)
for supported models, capability checks, credential overrides and context limits.

General model calls are separate from chat-model selection. `model_providers`,
`model_catalog` and `model_request` expose configured providers, current model
discovery and supported native API operations through Registry. A sandbox script
can opt into the credential-free `vwb_models` bridge with `models=true`; the host
makes each authorized request and retains generated files and response receipts
under the workspace. See [model access](MODEL_ACCESS.md) for provider and modality
limits.

Direct remote MCP servers are host connections, independent of Solstice. The UI
owns server configuration, OAuth consent and tool enablement. The official MCP
SDK performs discovery and authentication; host-only connection records preserve
grants across processes. Agents discover namespaced tool schemas through
`integration_tools`; reads use `integration_call`, while explicitly enabled
non-read tools use `integration_execute` and its separate policy capability.
Both remain inside Registry and import bounded results with provenance. Connection
generations fence late OAuth completions, and per-server locks serialize token
renewal and calls. See [integrations](INTEGRATIONS.md) for supported transports,
trust boundaries and provider registration requirements.

## Planes and trust (spec D02, I1-I4)
- **Control plane**: the `vwb` process. It alone holds vendor keys, writes the ledger, mints signed human events,
  runs the verifier and writes release packages. Agents reach it only through typed tool calls.
- **Agent plane**: model sessions that can only emit tool calls. Host-side agents (the Gemini and GPT loops, and the
  Claude fallback) have no shell and no file system beyond the work-folder file tools. Codex sessions (the job lead
  and the GPT workers) have a full shell, but inside a container that mounts only `work/`; the real OpenAI key never
  enters it (the proxy injects it).
- **Policy dial** (`config/policy.yaml`): one mode per capability. Local mode blocks raw shell and agent-written code
  on the host; Codex runs only in Docker. Roles limit which tools each agent sees.
- **Ledger** (`ledger/ledger.jsonl`): append-only and hash-chained. Flags, commits, renders, verdicts, questions and
  human events live here; flag state is a replay of the ledger. `ledger/tools.jsonl` logs every call with hashes.

## Supervised agents and open capabilities

Native creative applications are a separate, explicitly human-enabled host
capability. After Effects uses macOS AppleScript to dispatch snapshotted JSX and
an owned `aerender` process for rendering; durable supervisors retain receipts,
logs and cancellation state. Blender uses the official MCP SDK's stdio client
with a fixed, hash-locked MCP for Blender runtime and a loopback add-on. These
scripts run as the desktop user, not in Docker. Configuration is unavailable to
agent tools; reads and enabled writes still pass through Registry and the policy
dial. See [desktop applications](DESKTOP_APPS.md) for trust and lifecycle details.

The original flat swarm is now a bounded agent tree. `agent_spawn` creates a child;
children can delegate with the same tools. `agent_send`/`agent_inbox` carry peer
messages, `agent_list` reports task/status/tool activity, `agent_wait` supports
any/all completion with bounded waits, and `agent_interrupt`/`agent_followup`
control a subtree and continue a saved worker conversation. The older swarm tools
are aliases over this lifecycle. Workers share `work/`, so parents must assign
file ownership explicitly. Separate model contexts do not imply separate files.

Each child's effective tools are the intersection of its role and its parent's
effective tools. A worker cannot gain authority by spawning a broader role.
Only the main role commits video compositions. Default bounds are depth 3, 6
children per parent, 24 agents total and 6 concurrently active. A full tree
refuses a spawn promptly instead of queueing work behind waiting parents.
Status and messages are durable in `swarm/`, but execution is scoped to a main
turn. Workers are stopped when that turn ends; restart never silently relaunches
paid work. Tool side effects already completed are not undone by interruption.

Messages arrive before the next model call in API workers and at the next toolkit
result/inbox call in Codex or Claude SDK hosts. They do not alter an in-flight
model generation. Codex interruption removes its container; API cancellation is
cooperative between calls. Public worker activity is emitted separately from
the signed human ledger. Peer messages cannot grant permission or accept outputs.

`human_ask` defaults to non-blocking questions but can choose `mode="blocking"`.
`human_wait` waits on an existing ticket after independent work finishes. Waiting
is cancellable and bounded to an hour; a timeout leaves the question open without
assuming an answer. Renewable short leases identify live waiters. UI replies to
a live waiter feed the existing tool call, without interrupting or queueing a
duplicate turn. Expired leases return to normal queued-message delivery. Blocking
questions and permission/source-file questions never default. Answers remain in
the durable tickets and signed ledger, and inline replies appear in chat history.

`image_inspect` reads request images or asset revisions, crops/zooms within bounded
pixels and returns source coordinate transforms. Existing `media_frame` provides
video crop inspection. All providers can write small scripts through `file_write`
and run them using `sandbox_run`. The Docker image includes Python venv/pip and
build tools. `sandbox_environment` creates/reuses `work/.sandbox/envs/<name>` and
records `pip freeze`; direct `pip --target` installs can also live under work/.
Only work/ is shared, the container root is read-only, and no credentials or host
sockets are mounted. Network defaults to none. `network=public` separately passes
the `sandbox.network` policy gate and tunnels proxy-aware HTTP(S) over Docker stdio
to a public-destination proxy. DNS is pinned per connection; private, local and
special addresses are rejected. This works across Docker Desktop's VM boundary
without a bridge network. Git HTTPS works; raw TCP, SSH and private registries do
not. The main Codex host's existing model/tool gateway transport is separate.

Runtime is bounded to 30 minutes with 256–4096 MiB and 1–4 CPUs (defaults 512 MiB,
1 CPU). `background=true` returns a durable job supervised outside the chat worker;
`sandbox_status` pages logs, polls or cancels it across turns/reloads. Four jobs per
request, 8 MiB retained per output stream. A dead supervisor marks its job interrupted
without replaying the command. Background jobs survive chat Stop and require explicit
cancellation. Temporary tools are scripts, not trusted host handlers. Docker is
required and there is no host execution/installation fallback.

Chat attachments use immutable originals under `.attachments/<id>/` and independent
editable copies under `work/attachments/<id>/`. Public chat and queued messages store
IDs, SHA-256, MIME/kind, filename, byte count and work path; no base64 blobs. PDF/text/
image/unknown files do not become video sources. Valid video uploads retain source
indexing. `attachment_list`, `attachment_read` and `attachment_remember` provide
pinned inspection and persistent summaries. Unknown binaries are retained for an
appropriate sandbox parser, never automatically executed or unpacked.
Bulk path selections use `ChatTurn.context.folders/files`, independently of the
12 inline attachments. One folder is one reference, including nested files.
`LocalImports` snapshots explicitly selected host paths into private staging,
then atomically publishes independent copies under `work/imports/<id>/`.
APFS uses descriptor-based copy-on-write clones where available; fallback copies
check available disk space and source identity/size/timestamps. No host path is
mounted into the agent container. Import manifests stay outside `work/`;
directory hierarchy and relative work paths persist across turns and compaction.
This route does not inline pixels, pin immutable attachment originals, or eagerly
index videos. Agents enumerate and inspect the selected batch using their
existing file, image/PDF and sandbox tools.
Ordinary text reads scan at most 8 MiB with bounded line/output lengths; large-file
editing belongs in streaming sandbox scripts. Discovery stops at 500 matches or
10,000 scanned entries and prunes venv/package caches unless explicitly named.

Image payloads are cached from pinned originals, with integrity checks on reuse,
40 MP/50 MiB source bounds, 2048 px/4 MiB output bounds, and a 16 MiB aggregate
uploaded-image budget. Oversized images remain attachable for sandbox conversion. Newer images take priority;
omitted images remain explicit retrievable references. Direct provider histories
store logical image refs and hydrate them at the request boundary. OpenAI Responses,
Anthropic Messages, OpenRouter and Claude SDK receive native image blocks; Codex
receives read-only payload mounts via `--image`, including resumed sessions. Native
hosts own their internal transcript format. Model capability checks keep unsupported
images as references. Saved summaries can replace uploaded pixels during API replay.
Native compaction checkpoints the catalog without changing chat/files; original
images can always be reopened through tools. Other providers still have no manual
native compaction operation.

`pdf_info`, `pdf_read`, `pdf_search` and `pdf_render` share the preview's two-worker
limit and inspect PDFs in bounded disposable PDFium processes. Inputs are pinned
no-follow descriptors, limited to 64 MiB/10,000 pages. `attachment:<id>` reads the
immutable original even if its working copy changes. Text and literal search have
continuation cursors; renders include crop/zoom transforms. Explicit OCR uses Apple
Vision when available, with a three-page limit. No JavaScript/forms/embedded-file
execution is enabled. This is resource/process isolation, not a security VM for a
native parser. Structured table extraction and exact reading order are not claimed.

General HTML review artifacts are independent of `vwb.canvas/2` compositions.
`artifact_publish` snapshots authored HTML and records explicit local assets plus
stable review item IDs. A sandboxed canvas tab can arrange any gallery, chart,
video set or custom interaction. Its bridge stages item/region/time selections;
only the host's user action submits feedback into the normal message queue.
`artifact_feedback` exposes durable, version-specific feedback to agents. Remote
resources and host API access are unavailable to the HTML; changed assets require
republishing. See the authoring guide returned by `artifact_list`.

## The request workspace (`runs/<request>/`)
| Folder | What | Who writes |
|---|---|---|
| `request.json` | the request, job spec, chat turns | control plane, `job_spec` |
| `media/` | immutable source copies (hard links) | intake |
| `index/` | the edit index (SQLite), dossier, audio facts, readings | index builder |
| `work/` | the agents' editor: `outputs/<id>/canvas.json`, notes, drafts, advisor results | agents (file tools, Codex shell) |
| `work/studio/` | uploaded inputs and per-job image/video outputs, input snapshots and generation receipts | studio control plane; readable by agents |
| `.studio/` | private request-local job records, upload receipts and canonical staging | studio control plane only |
| `copy/deck.json` | every typeset string with provenance | `copy_add` and the compiler only |
| `canvas/<id>/v<N>.json` | immutable committed versions with HEAD | `canvas_commit` (job lead) |
| `plates/` | Remotion alpha plates by content hash | render |
| `renders/<id>/v<N>/` | MP4, audio, receipt | render |
| `qc/<id>/v<N>/` | verification, cue sheet, delivery note, signed manifest | verifier |
| `assets/` | registry of recovered, extracted, generated and uploaded assets (immutable revisions with lineage and measurements) | asset lab, generative tools, uploads |
| `asks/` | question tickets and remembered answers | broker; answers via the CLI |
| `ledger/`, `swarm/`, `memory/` | ledger and call log; mailbox and worker transcripts; plan and notes | control plane, agents |
| `release/` | accepted outputs with signed release records | `vwb accept` |

## Direct generation studios

The browser's image/video studios submit request-local jobs to `studio/service.py`
without starting an agent turn. Each job runs in a separate host process through
`studio/worker.py`, which calls the existing generation tools through
`Registry.call`. Role checks, `gen.image`/`gen.video` policy, call logs and asset
provenance therefore also apply to direct UI generation. Local background removal
uses the existing matte tool without a provider call; it retains source RGB/alpha
and registers a separate full-source inverse-alpha mask as segmented. The mask
and pinned source can be reused for an explicit inpaint job. `StudioGateway` pins a
compatible configured model for the job; it does not silently fall back to a
different model. Vendor keys remain in the host gateway.

Inputs are validated and snapshotted before dispatch. Uploaded and registered
sources must still match their receipts. Public `work/studio/` files are published
through pinned no-follow directory descriptors; private staging and durable job
state remain outside `work/`. Each result has an asset reference, file hash,
measured media metadata and a receipt linking its controls and sources. Exact-mask
studio inpaint preserves pixels outside the submitted mask. Local segmentation
is approximate; model-based subject extraction remains a generative recovery,
with its fidelity limits retained and no source-aligned mask inferred from its
bounding-box registration.

Studio jobs have independent idempotency keys, bounded concurrency, cancellation,
a parent-liveness pipe and a host deadline. Browser reload reconnects to saved
job state. A server restart never replays paid work: unfinished saved jobs become
interrupted and require a new explicit submission. Local cancellation cannot
revoke a provider request already accepted remotely. See [the studio UI and API
guide](UI.md#image-and-video-studios) for operations and capability limits.

## The edit index (spec D19, D35)
Eager, deterministic facts first; model readings stored as claims beside them; every row has a producer and status.
Stages (cached, cascading): probe, audio facts, shots, Vision pass (faces, people, saliency every 0.2 s; OCR on every
visible change and at least once a second), text tracks with a second OCR reader and roles, layout blocks, two
transcript readers with word-level consensus and pause snapping, a Gemini reading of the whole video, hazards, marks,
a GPT reading from text and contact sheets, and their disagreements. The dossier is the compact view agents read
first; SQL, search and frame tools give the detail.

## Composition: the canvas is code (spec D37, revised)
Source video is optional at intake. A request may begin with a brief or authorized
design reference. Those requests skip video indexing and use `time_map: []` plus
an explicit `duration_frames`; `canvas_new(duration_s=...)` creates that clock.
The compositor renders its image/clip/shape/text layers directly and the audio
renderer supplies silence. Imported raster designs receive immutable asset refs
with external-source lineage. Source video edits retain their span-based clock
and source narration. Both modes use the same commit, render and review pipeline.

There are no templates. A canvas (`vwb.canvas/2`, see `docs/CANVAS.md`) is an open scene graph the agent writes:
video windows (crop and box keyframes, masks, effects), images, shapes, text and captions, in any arrangement and
motion. Text comes only from the copy deck. The agent edits the file like source code, then:
1. `canvas_check` lints it (schema, references, squash, safe zone) and measures it against the index (what each
   window keeps, cuts and drops; type size; faces; motion), like a type checker plus a profiler;
2. `canvas_preview` renders chosen frames (text plates included);
3. `canvas_commit` snapshots an immutable version, expanding what code derives (copy entries, caption pages).
Advisors compute options, never decisions: `focus_find` (what each beat refers to), `reframe_solve` (a crop track
for any box), `clip_*` (cuts).

## Rendering (spec D20)
The compiler plans layers bottom to top. Video and image layers are composited in Python (frame-exact ffmpeg decode,
BT.709, OpenCV warps with Lanczos for upscales). Runs of text, caption and shape layers are rendered once by a generic
Remotion Scene component as an alpha plate (ProRes 4444), cached by the hash of its props, and composited at their
z-position. x264 encodes every pixel once; audio follows the time map with sample-exact trims, seam fades and
two-pass EBU R128 to -14 LUFS. A receipt records input hashes, tool versions and settings.

## Checks and delivery (spec D03, D12, D13, D40, D42)
The inspection engine is shared code that the editor's critic uses now and the judge will use later. T0 lint covers
delivery and degenerate edits. T1 replays every content window against the index, reads every typeset string back
with two OCR engines, re-renders sampled frames to attest the file, and transcribes the output audio. The verifier
adds standing rules (source usage terms, job codes, recovered marks, retyped fonts, unverified copy, unqualified
checks, egress), raises flags with code-set severity, computes triage, and signs a manifest. v0 cannot be GREEN.

## A chat turn, end to end
`vwb chat` creates or opens the request, builds the index, runs one turn of the main agent with the message, then
guarantees that every committed head version is rendered and verified (the best attempt always goes to review), and
builds the review page. `vwb chat -i` keeps the conversation open for many messages; `vwb chat --request <id> "..."`
sends one more message to it.

A request is one conversation, like a Cursor or Codex chat. The main agent's Codex home is mounted from the request
folder (`agent/codex/`), so the first message starts a thread and every later message resumes it (`codex exec
resume`) with everything the agent said, called and saw. Only the new message is sent, with `[harness]` notes for
what people did since the last message (answers, acknowledgements, waivers, accepts). `memory/lead_session.json`
keeps the thread per host and model. A host error resumes the thread once; a fallback host continues on its own
thread (told the turns it missed) or from a FULL wake (plan, notes, outputs, answers). One turn runs at a time per
request (`turn.lock`). Workers live within a turn: the main agent collects what it dispatched before it replies, and
the end of the turn stops the rest.

## Mapping to the spec
| Spec | Here |
|---|---|
| D05 main agent on the Agent SDK | `agents/job_lead.py` |
| D06/D07 workers, swarmd | `agents/swarm.py`, `agents/loops.py`, `agents/host_codex.py` |
| D10/D15 registry, late binding | `workspace/registry.py`, `assets/lab.py` |
| D13 flags | `config/flag_kinds.yaml`, `workspace/flags.py` |
| D19/D35/D36 edit index | `index/` |
| D20/D37 canvas and render | `canvas/`, `render/`, `generators/remotion/` |
| D21 text path | `canvas/copydeck.py`, `qc/inspect.py` (read-back) |
| D40/D42 editor QC, shared engine | `qc/` |
| D41 judge | not built (see `docs/ROADMAP.md`) |
| D43 human.ask | `ask/broker.py` |
