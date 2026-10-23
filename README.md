# Video Workbench

A chat-based video editor built as a general agent harness, for pharma video. You chat with one main agent (GPT-6
Astra in Codex) as you would with a coding agent, and follow-up messages continue the same conversation. The main
agent plans the edit and does the work itself: it rebuilds the source's elements for the new frame with vector
graphics, typeset copy, an image model (extract, create, inpaint, outpaint) and a video model (keyed motion), and
writes each output as an editable composition file. It spins up workers only when it chooses (Gemini 3.8 Flash
watches video natively; GPT-6.1-Sol composers, planner and critic run in Codex containers). The harness renders,
measures and checks every version, flags what it cannot prove, and a person accepts.

The promise: **text-exact, visually faithful, diffs flagged.** Typeset text comes only from on-screen text or the
transcript (each read by two engines); models never draw text. Source pixels change only through declared transforms
or registered edits, and every generated or regenerated asset is flagged with its lineage. Every check result is
evidence in a hash-chained ledger; models nominate, code decides, people accept.

```
vwb chat "Make 10 youtube shorts from this: /path/to/source.mp4"
vwb chat -i -r <request-id>      # keep talking to the same agent: "make short-02's headline bigger"
```

## What is in this build (v0 slice)
- **Edit index**: shots, a two-reader transcript with word-level consensus, two-reader OCR text tracks with roles
  (claims, footnotes, job codes, usage terms), faces, saliency, layout blocks, audio facts, two whole-video readings
  (Gemini and GPT), hazards and brand marks, in one SQLite evidence store with a compact dossier.
- **An open canvas**: an agent-written scene graph (images, keyed clips, vector shapes and paths, text, captions,
  source windows; keyframed boxes, crops, colours, rotation, path drawing, masks, effects). No templates and no house
  style; a 16-family open font library; advisors suggest cuts and crop tracks, the agent decides.
- **Generative tools**: `sunburst_create`, `sunburst_extract`, `sunburst_inpaint`, `sunburst_outpaint`
  (gpt-image-2.5-sunburst) and `omni_video` (gemini-omni-1.1-flash, green-screen keyed to alpha), each measured and
  registered with its lineage; the verifier raises the provenance flags.
- **Renderer**: frame-exact decode, a deterministic compositor, a generic Remotion scene renderer for text plates, x264,
  two-pass loudness, receipts.
- **Inspection engine and verifier**: delivery and degenerate-edit gates, what every window keeps, cuts and drops,
  OCR read-back of every string, re-render attestation, audio read-back, standing regulatory rules, flags with code-set
  severity, triage, cue sheets, delivery notes and signed manifests.
- **Main agent and optional swarm**: a GPT-6 Astra main agent in Codex with one persistent thread per request
  (fallbacks: the GPT loop, then Claude Fable 5.1 through the direct Anthropic API); a Gemini scene-miner and GPT-6.1-Sol planner,
  composers and critic in Codex containers, dispatched only when the main agent chooses; `human.ask` with a
  non-blocking broker; a review page per request.
- **Recipes**: Clip, Resize and the N-Shorts chain, as skills (`recipes/`). Any other request runs in open mode on the
  same tools.
- **Public web research**: `web_search`, `web_fetch` and bounded `web_research` through OpenRouter, with host-held
  credentials, source links and logged usage. See [web tools](docs/WEB.md) for setup, limits and alternatives.
- **Model access**: discover configured OpenAI, Google, Anthropic and OpenRouter models and call supported
  text, image, audio, video and embedding APIs through tools or sandbox Python scripts, with host-held keys.
  See [model access](docs/MODEL_ACCESS.md) for discovery, examples and output files.
- **Repository navigation**: shared bounded ripgrep, hash-aware source reads, live Git diffs/history,
  Python/JS/TS/TSX language-server navigation and focused saved-file context. Optional read-only explorer
  children use the same tools. See [code intelligence and setup](docs/CODE_INTELLIGENCE.md).
- **Workspace canvas**: collapsible chat, a file explorer, closeable tabs and two viewing groups for videos,
  images, interactive local HTML, paged PDFs, text and the shared browser. The Python harness remains unchanged.
- **Shared browser**: the main agent controls an isolated headed Chrome session through typed tools. The canvas
  streams that same session, with tabs, a Solstice cursor and explicit human takeover.
  Headless mode and host-configured remote CDP are also available. See [browser setup and research](docs/BROWSER.md).
- **Plugins and MCP**: select integrations in the composer. Solstice supports persistent host-owned OAuth and
  approved read tools; Figma imports design JSON and previews through the optional Solstice gateway extension.
  [Connection setup and Figma options](docs/INTEGRATIONS.md) describe the required registrations and deployment.

## Set up frontend and backend

The complete application is in [Solstice-Health/video-workbench](https://github.com/Solstice-Health/video-workbench).
The Python server includes the React/Eeva frontend. They ship together and use one
origin; there is no separate frontend build or server. Use macOS on Apple Silicon
for the full video-indexing workflow. Install uv, Node 22 or 24 with npm, FFmpeg and
Docker Desktop, then:

```bash
git clone git@github.com:Solstice-Health/video-workbench.git
cd video-workbench
./scripts/setup.sh --with-agent --with-browser
.venv/bin/vwb ui
```

Add the provider keys you need in `.env` or **Models and APIs → Provider APIs**.
For an initial UI-only installation, use `./scripts/setup.sh --ui-only`. Setup
preserves existing environment files and refuses to replace dependencies while
the server is running. [Complete setup, remote access, updates and rollback](docs/SETUP.md).

### Open the chat workbench

```bash
uv run vwb ui                       # http://127.0.0.1:8765, opens your browser
uv run vwb ui --no-open --port 8765  # start without opening a tab
```

The Eeva frontend opens your existing conversations, rendered videos and review
flags. It supports source uploads, live provider activity, expandable thinking
summaries and tool calls, queued messages, nonblocking questions, and a context
usage popover with native Codex compaction. Send queues while a turn runs;
**Send now** interrupts it and continues the same conversation. Stop pauses the
queue; **Resume queued messages** starts it again. Loading the UI does not start
an agent turn. See [the UI guide](docs/UI.md) for behavior and verification limits,
and [Eeva integration](docs/EEVA.md) for the design system.

Use the left rail to switch between conversations and workspace files. Open files
in the canvas, split it into two groups, or collapse chat for more viewing space.
PDF pages render locally, so no browser PDF plugin is required. The composer’s
**Plugins** menu selects integrations; **Manage connections** handles sign-in.

To enable the shared browser, install the pinned native runtime once:

```bash
uv run python -m vwb.browser.runtime install
```

Chrome must be installed on the host; headed Linux also needs a display server. Open a conversation and choose
**Browser**, then **Open browser**, or ask the agent to browse. **Take control** enables your pointer, typing and
navigation; **Return to agent** invalidates its old observation before it can continue. Closing the canvas tab keeps the
session; **Close browser** stops it. Graceful UI shutdown closes UI-managed local browsers while retaining their private
profiles. An independently started CLI browser is not adopted by merely viewing it. Remote sessions require
`VWB_BROWSER_CDP_URL` on the host; closing the local client does not stop a provider's billing or remote browser.

The composer model picker switches the main agent and exposes context, effort and
supported speed modes. Models and APIs manages visible models and provider keys.
See [model and API settings](docs/MODELS.md) for available IDs and runtime behavior.

Keys come from the nearest `.env` above the repo (or `VWB_ENV_FILE`); see `.env.example`. Provider keys saved in
Workbench override these baseline values through a private host file. The Claude fallback uses
the direct Anthropic API and requires an Anthropic key. Local hardware for full video indexing: Apple Silicon
(MLX Whisper, Apple Vision).

After a run:
```bash
uv run vwb status <request>              # outputs and triage
uv run vwb flags <request>               # open flags with ids
uv run vwb answer <request> <ticket> <option>
uv run vwb ack <request> <flag-id>       # or: vwb waive ... --second <name> for BLOCK
uv run vwb accept <request> <output> --by <name>
uv run vwb chat --request <request> "Short 3: open on the statistic instead"
```
The review page is `runs/<request>/review/index.html`.

## Repository map
| Path | What |
|---|---|
| `src/vwb/index/` | the edit index (video indexing) |
| `src/vwb/canvas/` | canvas schema, compiler, copy deck, versions |
| `src/vwb/render/` | compositor, Remotion plates, audio, pipeline |
| `src/vwb/qc/`, `src/vwb/verify/` | inspection engine (shared with the future judge), verifier, documents |
| `src/vwb/clip/`, `src/vwb/reframe/` | advisors: cuts, focus, crop tracks |
| `src/vwb/tools/` | the typed toolkit and the tool gateway (`vwb tools` lists the current tools) |
| `src/vwb/browser/` | pinned native driver, request-scoped sessions, ownership, observations and live stream relay |
| `src/vwb/web/` | local HTTP control plane, Eeva workspace UI and bounded file/PDF previews |
| `src/vwb/integrations/` | host-owned OAuth, Solstice MCP transport, approved reads and evidence imports |
| `src/vwb/agents/` | job lead host, worker loops, swarm, Codex-in-Docker host, prompts |
| `src/vwb/gateway/` | model gateway, Gemini and OpenAI providers, container services |
| `config/` | model slots, policy dial, levers, flag catalog, platform profiles |
| `recipes/` | skills: clip, resize, shorts |
| `generators/remotion/` | the generic Scene renderer for text, captions and shapes |
| `docker/codex-agent/` | the agent-plane image for Codex workers |
| `docs/` | architecture, canvas reference, tools checklist, decisions, roadmap |

## Status and limits
v0 outputs cannot be GREEN: no check is qualified on seeded defects yet. The asset lab has rungs 0-1 only; web scouts,
the bulk engine, the chat comment loop and the sealed judge are planned (`docs/TOOLS_CHECKLIST.md`). Licences of
Remotion and other components are to be reviewed before any commercial use.
