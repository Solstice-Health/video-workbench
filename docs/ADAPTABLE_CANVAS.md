# Adaptable canvas: implementation and design evidence

Feature branch: `codex/feat-adaptable-canvas`, forked from QC commit `cb9fe97`.
Research checked 2026-10-29. QC development is paused for this branch; its existing
features remain available.

## What this increment implements

One editable `vwb.canvas/2` document, one selected renderer per output, and a shared
Python media compositor. Image/video/clip decoding, z order, assets, copy provenance,
versioning, audio and output verification remain common. Text/vector runs become
transparent plates through a selected trusted adapter:

| ID | Implementation | Supported plate content |
| --- | --- | --- |
| `remotion` | Existing Remotion 4.0.534 React composition | Shapes, text, captions |
| `browser` | React 19.3 HTML/CSS typography and SVG, explicit frame clock | Shapes, text, captions |
| `gsap` | Same HTML/SVG components; paused GSAP 3.15 timelines evaluate keyframe easing | Shapes, text, captions |
| `canvas2d` | Native Canvas 2D / Path2D rendering | Shapes only; text/captions rejected |

GSAP is a motion engine, React is an authoring/component system, and HTML/SVG or
Canvas 2D are drawing surfaces. The IDs identify complete adapter combinations,
not interchangeable names for the same technology. The GSAP adapter currently
maps the canvas's existing easing vocabulary; it does not expose arbitrary GSAP
plugins or author-defined timelines. A swap preserves document structure but may
change rasterization, text fitting or spring motion between different adapters.
Preview the result before committing it.

`canvas_renderers` and `GET /api/canvas-renderers` expose the catalog.
`canvas_new(renderer=...)` selects an adapter. `canvas_set_renderer` changes only
an existing working file and rejects incompatible plate layers. `canvas_check`
also detects an incompatible renderer or unavailable pinned revision. Commit
pins the renderer revision; cache keys include that pin and resolved props.
Legacy canvases without a renderer keep the original Remotion cache identity.
New browser-backed previews and commits also capture the actual runtime: adapter
sources, package lock, font files, Chromium and FFmpeg binaries, and OS/architecture.
That digest participates in plate identity and is saved in the committed canvas
and receipt. A different environment requires a new commit or restoration of the
old environment; it cannot silently reuse the old plate. Existing finished
renders remain viewable. Media-only documents need no browser environment.
Rendered receipts name the selected renderer and its pin.

The browser adapters execute repository-owned components only. Canvas strings
cross as data, not executable source. They reject unknown fields, CSS URLs and
expressions, external resources, unlisted fonts, unresolved SVG references and
unsupported layer types. Headless Chromium receives no model credentials; its
requests are restricted to a private local bundle and pinned fonts. Navigation,
websockets and service workers are blocked. Frames are sought explicitly, fonts
are awaited, and background animations/tickers cannot drive export timing.
Bounded jobs publish only verified RGBA stills or alpha ProRes plates; failures
leave no reusable partial cache. Runtime render calls never install packages.

This is a trusted renderer adapter API, not arbitrary HTML/JS generator
registration. Agent-authored programs continue to use the existing sandbox;
HTML workspace artifacts retain their separate preview workflow. No Fabric,
tldraw or Three.js editor is embedded in this increment.

## Composer behavior

Ask / Storyboard / Agentic appear beside the chat input and persist per
conversation. **These modes are UI previews only.** They do not change agent
permissions, tool access or execution. In particular, Ask is not enforced read-only.

The Canvas picker defaults to Auto. One or multiple choices mean preferred
allowed adapters for the agent to choose from, one per output. Explicit choices
add a visible instruction to the saved user message. They are guidance, not a
policy enforcement boundary. Auto adds no instruction; `canvas_new` still defaults
to Remotion when the agent omits its renderer argument. Human-question answers
and QC messages do not receive the renderer instruction.

## Research decisions

There is no demonstrated universal “best canvas for agents” in the sources below.
The choices here favor explicit state, editable identities, deterministic time
sampling, typography/vector fidelity, repeatable export and manageable integration.
These are engineering judgments from primary documentation, not a comparative
performance benchmark run by this project.

- **SVG/DOM first:** native structured elements fit the current layer/copy model
  and avoid importing graphics through a second scene representation. Browser
  HTML is useful for typography; arbitrary HTML is not automatically portable SVG.
  [SVG 2](https://www.w3.org/TR/SVG2/),
  [explicit animation time](https://www.w3.org/TR/web-animations-1/#dom-animation-currenttime).
- **GSAP for controlled motion:** paused timelines can be sought explicitly.
  This supports frame sampling without relying on wall-clock animation.
  [GSAP totalTime](https://gsap.com/docs/v3/GSAP/Timeline/totalTime()/).
- **Fabric.js is the strongest next candidate for direct 2D object editing:**
  JSON objects and custom IDs can persist, with SVG import/export. Its documentation
  explicitly warns that SVG round trips are not exact. Build a measured import/edit/
  export spike before adopting it as the document authority.
  [Fabric core concepts](https://www.fabricjs.com/docs/core-concepts/),
  [custom properties](https://www.fabricjs.com/docs/using-custom-properties/).
- **Konva is an alternative interaction layer:** useful for custom transforms and
  selections; its guidance favors application-owned state for complex apps. It
  does not offer native SVG export. Do not introduce both Fabric and Konva initially.
  [Persistence](https://konvajs.org/docs/data_and_serialization/Best_Practices.html),
  [SVG capabilities](https://konvajs.org/docs/guides/why-konva.html).
- **tldraw is an editor-shell candidate:** the official agent starter combines
  screenshots, selected shapes, structured scene data and validated actions. Those
  patterns transfer well even without embedding its whiteboard SDK. It is not the
  offline video renderer. [Agent starter](https://tldraw.dev/starter-kits/agent),
  [export behavior](https://tldraw.dev/sdk-features/image-export).
- **PixiJS is a later graphics-throughput option:** controlled ticking and pixel
  extraction are useful for dense animated scenes. Its June 2026 graphics-to-SVG
  support exports graphics instructions, not an entire textured scene. Profile
  real workloads before adding another engine.
  [Ticker control](https://pixijs.com/8.x/guides/components/application/ticker-plugin),
  [June 2026 release](https://pixijs.com/blog/june-2026),
  [SVG export scope](https://pixijs.download/dev/docs/scene.graphicsContextToSvg.html).
- **Three.js adds a distinct 3D capability:** explicit animation seeking supports
  deterministic scene state. Its SVGRenderer cannot preserve textures, shadows or
  advanced shading. Add when 3D authoring is required, rather than routing 2D boards
  through it. [AnimationMixer](https://threejs.org/docs/pages/AnimationMixer.html),
  [SVGRenderer limits](https://threejs.org/docs/pages/SVGRenderer.html).
- **Motion Canvas is a specialist candidate for explanations:** code-authored
  sequences and image/video exporters are attractive, but upstream headless export
  remains a draft integration that needs a working automated spike.
  [Rendering](https://motion-canvas.io/docs/rendering/),
  [headless implementation status](https://github.com/motion-canvas/motion-canvas/pull/1055).
- **Theatre.js belongs in a future timeline editor:** project JSON, sequenced
  properties and custom RAF control can animate another rendering surface. It
  is not itself the drawing/export backend.
  [Core API](https://www.theatrejs.com/docs/latest/api/core),
  [advanced clock control](https://www.theatrejs.com/docs/latest/manual/advanced).

Licensing is part of adapter selection. Fabric, Konva, PixiJS, Three.js and Motion
Canvas publish MIT code. tldraw's SDK requires appropriate production licensing;
MIT starter examples do not change that. Theatre core is Apache-2.0 but Studio is
AGPL-3.0. GSAP uses its own no-charge license with restrictions covering certain
competing visual animation builders; it should not be represented as MIT or as
unconditionally usable in any future embedded visual editor.
[Fabric license](https://github.com/fabricjs/fabric.js/blob/master/LICENSE),
[tldraw license](https://tldraw.dev/community/license),
[Theatre package licenses](https://github.com/theatre-js/theatre#license),
[GSAP terms](https://gsap.com/community/standard-license/).

## Recent agent research applied here

**DrawAI (August 2026)** separates fidelity from editability and evaluates
reconstruction as a structured planning plus code/render/revise process. The
transferable point is to keep text, shapes and localized assets manipulable;
a visually plausible bitmap is insufficient evidence of authoring quality.
This motivates retaining the common editable document and copy deck through
renderer swaps. Its benchmark results are not evidence that a particular Workbench
backend wins. [Paper](https://arxiv.org/html/2608.00548v1).

**Vector-Bench (July 2026)** evaluates requested repairs and preservation of
unrequested structure separately. Its 40-task SVG study reports much lower full
specification success than partial repair progress. This motivates exact structural
preservation tests for renderer swaps and immutable earlier versions, in addition
to image checks. It does not establish a general renderer ranking.
[Paper](https://arxiv.org/html/2607.19056v1).

**SVGenius (June 2025)** covers understanding, editing and generation across
complexities; the reported degradation as drawings grow more complex argues for
small, addressable layer edits and bounded visual evidence instead of repeatedly
regenerating whole compositions. This is an engineering inference, not a tested
model-quality improvement in this branch. [Paper](https://arxiv.org/abs/2506.03139).

No model training, RSI promotion or new QC judge is introduced here. A later
agent-quality study should compare edit success, protected-object preservation,
visual fidelity, editability, runtime and cost on the same briefs and references.

## Setup and validation

From `generators/remotion/`:

```sh
npm ci
npm run setup-fonts
npm run setup-browser
npm test
VWB_RENDER_SMOKE=1 npm test
./node_modules/.bin/tsc --noEmit
```

The browser adapters can also reuse an already-installed Remotion headless
Chromium, or an explicit host `VWB_CHROMIUM_EXECUTABLE`. Remotion's own renderer
uses its existing browser setup (`npx remotion browser ensure`). The shared Node
runtime remains in `generators/remotion/` to match repository layout conventions;
the alternate browser bundle has a test preventing Remotion imports.

From the checkout:

```sh
uv sync --frozen
uv run pytest
VWB_RENDER_SMOKE=1 uv run pytest tests/test_canvas_renderers_live.py
uv build
```

The real tests cover nonconsecutive/repeated frame seeking, offset clocks, loaded
fonts, alpha, exact frame counts, all four adapters, network rejection, cleanup,
Registry tool dispatch, preview/commit/full export, renderer swap and previous
version preservation. Cross-machine byte identity is not promised: OS, Chromium,
fonts and graphics hardware affect rasterization. Pin the environment for strict
pixel comparisons. [Playwright snapshot guidance](https://playwright.dev/docs/test-snapshots),
[Remotion deterministic-frame requirements](https://www.remotion.dev/docs/flickering).

The agent-visible host dependency-repair workflow remains deferred in ROADMAP.md.
