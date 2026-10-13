# Build decisions

Decisions taken while building this v0 slice, with the reason for each. Spec decisions (D-numbers) are in the Video
Workbench spec; this log covers how this build realises them and where it departs.

**B01. Composition is model-authored; no templates (2026-10-09).** The first cut placed every output into one fixed
"band" layout and let the agent choose only a clip, a title and a logo. That contradicts a general harness. The canvas
became an open scene graph (`vwb.canvas/2`) the agent writes like code; the framing solver and clip engine became
advisors that compute options and measurements for whatever the agent designs. Why: the product is a Cursor/Codex-like
editor for video, so arrangement in space and time must be the model's decision, checked by code.

**B02. Codex workers run in Docker (user decision, 2026-10-09).** GPT-6.1-Sol roles (planner B, composer, critic) run
`codex exec` in one container per task. The container mounts only `work/`, reaches the model through a control-plane
proxy that injects the key (I3) and logs every call (I4), and reaches the toolkit over MCP/HTTP with a per-run token.
Why: reuse Codex's agent loop, shell and patch tools where they help (editing composition files), inside a sandbox, in
line with the spec's agent plane. Launching a Codex agent on the host without a sandbox was refused by the operator's
safety policy in this build session; the container setup was the user's chosen alternative.

**B03. Host-side agents are tool-restricted in local mode.** The Claude job lead and the harness loops get no shell
and no file access beyond the work-folder file tools. The policy dial blocks `shell.exec` and `code.run` until agents
run in a microVM (spec D02). Why: no microVM on a laptop, and every action then passes the tool gateway.

**B04. The main agent uses the local Claude login.** The `ANTHROPIC_API_KEY` in the provided `.env` was rejected
("invalid x-api-key"). The Agent SDK's bundled CLI (2.1.294) supports `claude-fable-5-1` and authenticates with the
local Claude Code login; the session environment is scrubbed of other vendor keys. For hosting, set a valid key.

**B05. Decode through the system ffmpeg, not PyAV.** OpenCV and PyAV each bundle FFmpeg libraries, and macOS warned of
duplicate classes that "may cause mysterious crashes". Frame-exact reads use a half-frame input seek and explicit
BT.709 conversion, verified against a sequential decode.

**B06. Two readers for speech and on-screen text.** Whisper (MLX, word times, a glossary mined from the slides) and
Gemini (audio only, so it cannot copy the slides); Apple Vision and RapidOCR on full frames. Disagreements are kept and
surface as TEXT-UNVERIFIED. The audio, not the model, places word boundaries (pause snapping).

**B07. Change-driven OCR instead of every-frame OCR.** OCR runs on every visible change and at least once a second
(207 of 935 samples on the test source). Every-frame OCR is a lever for later.

**B08. Text is never truncated.** The Remotion renderer shrinks type to fit its box (down to the layer's minimum) and
never clips characters; the verifier reads every string back with two OCR engines.

**B09. One verifier path, honest triage.** Checks are unqualified (no seeded qualification yet), so CHECK-UNQUALIFIED is
raised on every output and nothing can be GREEN, as the spec requires for v0.

**B10. Source usage terms are a BLOCK.** The test source states it is for US healthcare professionals and must be
viewed in its entirety. Every excerpt therefore carries SOURCE-TERMS (BLOCK, a recipe row added to the catalog) until a
senior pair or Regulatory Affairs rules. The outputs are still produced and reviewed (spec D14).

**B11. Captions are derived copy.** Caption pages are expanded from the two-reader transcript at commit time and
entered into the copy deck with their reader agreement; agents never type caption text.

**B12. Recovered marks keep their own background.** The rung 0/1 logo is stored as a rounded badge with its original
background (no invented matte), plus a keyed variant. NEEDS-SOURCE:logo stays until a master file is uploaded.

**B13. The OpenAI loop holds its own history.** The organisation runs Zero Data Retention, so the Responses API
refuses `previous_response_id`. The loop sends `store=false`, asks for `reasoning.encrypted_content`, and replays the
whole conversation each turn: reasoning items as encrypted content, calls and messages without server ids. Found when
the job lead fell back to GPT-6.1-Sol in the first end-to-end run and the fallback failed on its second turn.

**B14. The gateway bounds every tool result.** The first end-to-end run lost its Claude session to one 16-frame contact
sheet: the CLI echoes each tool result (with its images) on stdout, and the SDK's 1 MB message limit was exceeded.
Images are now fitted to 1568 px and 600 KB (models downscale past ~1.5k px anyway), the SDK buffer is 64 MB, and text
results over 60k characters are saved to `work/.results/` with their head returned, so an agent pages through them
like a file (Codex and Claude Code do the same). Tool JSON is compact: small records stay on one line.

**B15. Recover the session before switching models.** A host error resumes the same Claude session once (full
context, tool effects kept). Only a second failure hands over to the fallback model, which starts from a FULL wake of
the request so it does not redo finished work.

**B16. A turn's end stops its workers.** When the job lead's turn ends, running workers are cancelled between model
turns and their containers removed before the tool and model services shut down. In the first run an orphaned Codex
container kept calling a proxy whose process was exiting.


**B17. No house style anywhere.** The first agent runs all looked alike (blurred backdrop, logo badge, white headline
with an accent bar, karaoke captions, progress bar) because the resize recipe listed crop layouts as the menu, the
canvas reference showed one look in every example, the schema defaulted to that look, Montserrat was the only font, and
a QC rule raised UNREQUESTED-CHANGE whenever no source video window was shown. All five are gone: recipes and prompts
treat the source as material to rebuild, CANVAS.md examples are syntax only, text layers must choose font, weight, size
and colour, a 16-family open-font library replaces the single font, and TEXT-DROPPED now counts typeset copy as carrying
the source's text. The user's direction (2026-10-09): composition must be model-driven, with no templates or manual
assets.

**B18. Generative tools keep a hard boundary around source pixels.** gpt-image-2.5-sunburst masked edits also changed
pixels outside the mask in a probe (a typo appeared in body text: "pneumococccal"), and an unmasked outpaint moved and
shrank the kept image. So code, not the model, decides what survives: inpaint returns the original outside the
feathered mask, outpaint returns the placed image exactly (apart from a declared blend band that hides the seam), and
both report a seam error and any text the OCR still reads. Extraction is registered back to the source with SIFT and
RANSAC (the model may rescale what it redraws), so fidelity and placement are measured, not assumed. Every generated
asset is OCR'd and face-checked; the verifier turns the registry row into GEN-CREATED, GEN-RECOVERED, GEN-TEXT,
SYNTH-PERSON or MATTE-RISK for each output that uses it. Prompts forbid drawn text; typeset text is the only text.

**B19. Keyed motion: green-screen prompting plus a per-frame difference key.** Omni has no alpha channel. The element is
put on a key-colour plate chosen as the candidate (green, blue, magenta) farthest from the element's own colours, the
prompt holds the background flat, and every frame is keyed against its own measured border colour (the model drifted
the requested green by up to 30 levels in probes), with colour unmixing and edge spill suppression. Probe clips keyed
with interior holes at or under 0.2%. Clips are 3-10 s at 24 fps; the compositor maps them onto the 30 fps output clock.

**B20. The job lead runs on GPT-6 Astra in Codex.** The user asked for gpt-6-astra at max effort as the main agent and
chose the Docker setup for Codex earlier. The lead's container gets the same isolation as the workers (work folder only,
per-run token, key-holding proxy) plus the swarm on its MCP server, a 90-minute tool timeout for batch renders and asset
generation, and Astra's 1.05M-token window. A smoke test confirmed that images returned by MCP tools reach the model.
If the Codex host fails, the stateless GPT loop and then Claude Fable continue from a FULL wake.

**B21. Generative capabilities are allowed, not asked.** The spec puts generative recovery behind ask until X06/X07
qualify it. The operator asked for these tools to be used now, so the dial rows gen.image, gen.video and
asset.recover.generative are allow; the provenance flags still reach every output that uses a generated asset.

**B22. Plates cover only their frames; previews render stills.** The first 3-minute run showed two costs that grow
with length. Every run of text and shape layers was rendered as a full-length alpha plate (5,400 frames at 1080x1920,
85 s and 0.2-1.7 GB each), even when its layers showed for seconds. And `canvas_preview` rendered all of a canvas's
plates to show four frames (25+ minutes, queued behind any batch render on the same lock). A plate now covers only the
union of its layers' spans (local frame + offset = output frame; output pixels are identical, verified to max
difference 0), and a preview renders just the frames it shows as PNG stills on a lock of its own (14 s cold, 2 s
cached). A source-pixel strip (a matte cut-out placed from a source frame) now counts as carrying the text it shows,
so TEXT-DROPPED no longer fires on footnotes that agents lift instead of retyping.
Two fixes found while resuming that run: an extraction made from a cropped frame recorded its placement relative to
the crop (89 of 107 assets), so new rows say `placed_frame: source` and older rows are corrected when read (registry
revisions stay immutable); and image layers accept every still kind (recovered marks are `mark.badge` and
`mark.keyed`), not only `image`. The run was resumed with `vwb chat --request` after the job lead asked the operator to
restore the text renderer; the committed canvases, assets, plan and notes carried over.


**B23. Mattes take edge colours from the subject, not from un-mixing.** The critic found a yellow fringe around a
matte-cut logo: dividing by a rough alpha at soft edges amplifies error into the background's complementary colour.
The flat-background key now propagates the colour of solid subject pixels outward and puts each edge pixel's alpha on
the line from the background colour to that local subject colour, so edge pixels can only take colours the subject
has (tested on a white subject over saturated blue, and on the source's logo cards).

**B24. Charts may be recreated in any form; unlabelled values are measured, not forbidden.** The first rule ("never
values read off bar heights") stopped agents from rebuilding an unlabelled source chart, so they kept its small
original pixels. The user asked for recreations like 3D bar charts. Agents may now rebuild charts in any form (flat,
3D or isometric bars from path faces, rings, pictograms, animated builds). Printed values are used where the source
has them; otherwise `chart_read` measures the chart (bars by colour, the value axis fitted from OCR'd ticks, legend
series, category labels; R^2 0.99999 and about +/-9 units on the source's incidence chart). Measured values are typeset
as authored copy with a note naming the chart and frame; numeric authored copy counts as regulated, so it is flagged
HOLD until a person confirms it. Meaning stays fixed: no new claims, no dropped qualifiers.

**B25. A new format is a remake.** The 3-Shorts run still cropped slides into cards, pasted reference text as source
pixels at about 6 px cap height, and blew up 100 px source icons by up to 3.2x. Two causes: the recipe offered
"lift it (keeps the source pixels exactly)" as a faithful option, and tiny footnotes failed the two-reader agreement,
so retyping them meant unverified copy while pixels did not. The `resize` recipe became `remake`: every screen is
re-designed natively for the new frame, every word is retyped (references included), no source pixels are shown
above native size, charts are rebuilt, small art is traced or recreated, and motion and transitions come from the
video model. The prompts carry the same bar and a technique per element kind (principles, not layouts). Two new
checks make the defects visible while designing: source text inside image layers measured against the legibility
floor (fine print has a floor of its own), and UPSCALED for image layers.

**B26. A third reader for small text.** A vision model of another family (Gemini) reads tight, upscaled crops of every
line the OCR engines disagree on; two of three settle it. On the test source this settled 52 of 83 disagreements;
references reached 92% agreement and footnotes 81%, so they can be retyped with evidence instead of shown as pixels.
Crops are tight vertically, because the model otherwise reads a neighbouring line.

**B27. Vectors for flat art, mattes for people.** Flat icons and marks are traced (VTracer after k-means colour
quantisation, edge blends merged, fills snapped to the palette): the source's mark becomes 24 paths in its two brand
blues at IoU 0.93 and renders sharp at any size. Painterly art traces poorly and is recreated with the image model
instead. Speakers are matted with RobustVideoMatting (open weights, ONNX on the Neural Engine, 18 fps at 1080p),
cached per source span, restricted to regions anchored on the index's face boxes (the model also keeps logos as
foreground), and looked up by source frame so the cut-out stays in sync with the time map.

**B28. Generation only through the toolkit.** A containerised agent read Codex's bundled image-generation skill. It
then used `sunburst_create` (registered, flagged), but nothing stopped a direct call. The model proxy now refuses
`/images`, `/videos` and `/audio/speech`, and Responses requests that carry hosted tools (image generation, web
search, code interpreter, computer use); the refusal names the toolkit tool to use. Every generated pixel therefore
has a registry row, a lineage and a provenance flag. The proxy also records each call's model and token usage
(read from the last stream events), which `vwb spend` sums.

**B29. Canvases are written like code.** The job lead took 820 s for one model turn in the first remake experiment,
and another turn ran over 30 minutes, while it wrote whole canvases (hundreds of layers) as single JSON edits. The
Codex containers have Python 3, so the prompts now ask for a small generator script per output (shared styles,
loops over beats and items, timings from word times) that writes canvas.json and is re-run after each change.

**B30. Prompt experiments on the remake bar.** Experiment 1 (0:28-1:11, 42.6 s) on the remake prompts retyped every
string, traced every flat icon, rebuilt the unlabelled chart with `chart_read` values as bars that build with the
narration, and used a `shape_kit` ring; it generated no images because that span has only flat graphics. Its
weaknesses were scale (content huddled in the top half, 36 strings under the floor) and no motion beyond builds.
Two prompt additions followed: every screen needs a visual anchor, recreated at full resolution with the image
model when the narration's subject exists in the source only as a small icon, and animated with the video model
while it is spoken; and phone scale (large anchors and numbers, body text well above the floor, the whole height
used, split rather than shrink). Experiment 2 (1:06-2:04) recreated lung, heart and brain heroes from the source's
icons with `sunburst_create`.

**B31. The main agent does the work, in one continuing conversation.** The person's direction (2026-10-12): the
main agent performs the task itself and only chooses to spin up the swarm or a few workers when it judges them
necessary, and its chat with the person flows like a Cursor or Codex chat. The main agent's prompt and the Shorts
and Clip recipes no longer require a scene_miner, a planner_b, composers or a critic; the prompt says when each is
worth it, and that small requests and follow-up changes never need one. Each request is one conversation: the main
agent's Codex home is mounted from the request folder, the first message starts a thread and later messages resume
it (`codex exec resume`), with only the new message and `[harness]` notes (human events since the last message,
turns another host answered). Before this, every message woke a fresh session from the plan and notes, which lost
the agent's own reasoning, tool results and what it had looked at. A host error resumes the thread once; fallback
hosts keep their own threads. `vwb chat -i` keeps the conversation open in the terminal, and a per-request turn lock
refuses a second message while a turn runs. Verified 2026-10-12 on a fresh request: seven messages ran on one Codex thread (one
rollout file); follow-ups were answered from the thread's history alone ("of those sections", "what did I ask
first"), in 4-24 s each with no workers; a concurrent message was refused; Ctrl-C in `vwb chat -i` stopped a turn
mid-tool-call, removed its container, and the next message resumed the same thread knowing what was interrupted.

**B32. A turn counts only when it completes; recovery is matched to the failure.** Max-effort turns used to stall:
gpt-6-astra can think for over 30 minutes without a stream event, Codex's 5-minute default idle timeout took that for a
dead stream and restarted the turn, and the model proxy cut reads at 30 minutes. Codex's `stream_idle_timeout_ms` is
now 60 minutes on the gateway provider and the proxy waits 65 minutes (the person's go-ahead, 2026-10-12; reasoning
summaries stay off). Testing the recovery path found that a container dying mid-turn returned its last progress note
("I'll inspect the five frames...") as the reply; a turn now counts only on Codex's `turn.completed`. Failures are
handled by kind: part-way failure resumes the same thread once; a thread missing from the store ("no rollout found")
starts a new thread from the saved state rather than retrying; a host that cannot start (policy off, Docker down)
passes the turn to the next host at once and keeps its thread for the next message; a thread is dropped only after two
failed turns in a row, not after one, so a transient API error does not cost the conversation's memory. Run live:
a container killed after two tool calls (resumed, refetched the lost frames, finished in 29 s); Codex and then the GPT
loop made unavailable (the GPT loop, then Claude on a new thread, then Claude resumed, answered); Codex back (resumed its
own thread and listed the three turns it missed); a bogus thread id (new thread from the saved state in 10 s).

**B33. First production run under B31 and B32.** Request r-20261012-102618: 3 Shorts of about 30 s, no cropping, nearly
every element remade, summarized text, no captions, visual-first. The main agent did the whole job alone in one
64-minute turn (235 tool calls, no workers, no stream errors; its longest model call was 189 s, so this run did not
need the longer idle timeout). It recreated four hero illustrations from small source icons (image model, two
variants each), animated them with Omni on a key colour, traced the brand symbol, rebuilt the data as 3D bars, a
ring and a 50-dot pictogram, wrote 67 short copy entries that keep their qualifiers, and repaired three times
(overlap, type size, a static hold, an OCR mismatch). No output shows source pixels or captions. All three are RED, as
expected: SOURCE-TERMS, summarized copy that a person must confirm (TEXT-UNVERIFIED), slide text left off screen by
the low-text brief (TEXT-DROPPED), the job code. The brief and the harness pull against each other on references: the
agent kept full citations on screen and enlarged them for the legibility floor, so the citation block became each
screen's main text. Flag summaries for traced assets now name VTracer instead of "None".

**B34. Light sweeps and matte erosion are general capabilities.** The person asked for the source's 3D DNA ribbon to
be ported into the Shorts in three animated variants: a glare in the intro, a video-model remake on a green screen
with eroded edges (first and last frames from the image model), and one left to the harness. Two pieces were
missing and both are generic, so they went into the toolkit rather than into a recipe: `effects.sheen`, a soft band of
light that crosses an image, clip or video layer inside its own alpha (any element can catch a glare, with no video
model), and matte refinement for keyed clips, where `erode` pulls the edge in and `feather` softens it, available when
a clip is generated (`omni_video`) and afterwards (`clip_rekey`, which keys the green-screen original again without
paying for a new clip). Tested: a sweep crosses a recreated illustration only inside its shape and only within its
frames; a 3 px erosion re-keyed an earlier clip with a cleaner edge. The first real use found a keyer bug: the key colour was measured
as the median of the frame's border, so a full-frame background element (the DNA ribbon touching every edge) was
measured as the "key" and keyed itself out (0.01% coverage). The keyer now measures only border pixels near the
colour it asked the model for, and falls back to that colour when the subject fills the border; the same raw clip
then keyed at 20% coverage with clean edges and no holes.

**B35. Resumed threads hear what changed in the harness.** A long conversation outlives versions of the harness. The
first DNA follow-up showed the cost: the agent's knowledge of the canvas came from a turn before `effects.sheen`
existed, so for "a simple glare" it reached for the video model. Each thread now stores a fingerprint of what the agent
sees (every tool's description and schema, the canvas reference, each recipe, its own instructions); a resumed turn is
told the new, changed and removed tools and the references to read again, and gets the full new instructions if those
changed. Threads from before the fingerprint are compared by file time against their last reply. Run live: the
interrupted follow-up was resumed with a one-line clarification; the note named canvas_schema and the remake recipe,
the agent re-read both and used the light sweep. Also: a creation guided by source crops is now flagged as a redraw of
source content (GEN-RECOVERED:split), not as a new element (GEN-CREATED).

