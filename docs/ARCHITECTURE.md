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
                 (fallbacks: the stateless GPT loop, then Claude Fable 5.1 on the Agent SDK)
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

## The request workspace (`runs/<request>/`)
| Folder | What | Who writes |
|---|---|---|
| `request.json` | the request, job spec, chat turns | control plane, `job_spec` |
| `media/` | immutable source copies (hard links) | intake |
| `index/` | the edit index (SQLite), dossier, audio facts, readings | index builder |
| `work/` | the agents' editor: `outputs/<id>/canvas.json`, notes, drafts, advisor results | agents (file tools, Codex shell) |
| `copy/deck.json` | every typeset string with provenance | `copy_add` and the compiler only |
| `canvas/<id>/v<N>.json` | immutable committed versions with HEAD | `canvas_commit` (job lead) |
| `plates/` | Remotion alpha plates by content hash | render |
| `renders/<id>/v<N>/` | MP4, audio, receipt | render |
| `qc/<id>/v<N>/` | verification, cue sheet, delivery note, signed manifest | verifier |
| `assets/` | registry of recovered, extracted, generated and uploaded assets (immutable revisions with lineage and measurements) | asset lab, generative tools, uploads |
| `asks/` | question tickets and remembered answers | broker; answers via the CLI |
| `ledger/`, `swarm/`, `memory/` | ledger and call log; mailbox and worker transcripts; plan and notes | control plane, agents |
| `release/` | accepted outputs with signed release records | `vwb accept` |

## The edit index (spec D19, D35)
Eager, deterministic facts first; model readings stored as claims beside them; every row has a producer and status.
Stages (cached, cascading): probe, audio facts, shots, Vision pass (faces, people, saliency every 0.2 s; OCR on every
visible change and at least once a second), text tracks with a second OCR reader and roles, layout blocks, two
transcript readers with word-level consensus and pause snapping, a Gemini reading of the whole video, hazards, marks,
a GPT reading from text and contact sheets, and their disagreements. The dossier is the compact view agents read
first; SQL, search and frame tools give the detail.

## Composition: the canvas is code (spec D37, revised)
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
