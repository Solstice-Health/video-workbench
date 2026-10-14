# Chat workbench

Run `uv run vwb ui` from this repository. The UI is served at
`http://127.0.0.1:8765`; `--no-open` keeps it from opening a browser, and `--port`
changes the port. If the environment is already installed, `.venv/bin/vwb ui`
does not require uv's cache. The server listens only on loopback.

The application uses React 18 and the organization's Eeva components, fonts and
tokens. All browser assets are local. There is no frontend package installation
or build step. Application files live in `src/vwb/web/static/`; vendored sources
and licenses are under `static/vendor/eeva/`. See [Eeva](EEVA.md) and
[the design rules](../DESIGN.md).

## Conversations and sending

Open an existing conversation from the left navigator, or choose New conversation,
attach a source video, and describe the edit. The Outputs canvas tab shows the latest
committed version of each video, its real preview and flags, plus links to download
the video or open the complete review. Merely opening a conversation does not run
models or resolve old questions.

Each request still has one continuing agent thread. Messages are processed one at
a time under the existing turn lock:

- **Send** starts an idle conversation or queues a message behind its active turn.
- **Send now** stops the current UI-owned turn, waits for its process to finish
  cleanup, and puts the new message ahead of queued messages on the same request.
  Changes already made by tools remain in place.
- **Stop** interrupts the current turn and pauses queued messages. Queued messages
  can be removed or resumed explicitly. Restarting the server preserves pending
  messages and leaves them paused.
- A turn started in another terminal is visible, but must be stopped there. The
  UI can queue a message behind it.

Queues live in `memory/web_queue.json`, with worker receipts in `memory/web_jobs/`.
The server owns one worker process per active request. Parent-loss detection stops
orphaned workers. Abnormal cleanup only targets Docker containers labeled with that
UI job's identifier. A second UI server cannot dispatch the same runs directory.

## Models and provider APIs

The model pill below the input selects the main agent. Context and Effort open
side menus; the expand control opens Models and APIs to manage picker visibility
and OpenAI, Anthropic, Google and OpenRouter credentials. Changes affect future
submissions; active turns and already queued messages keep their captured settings.
See [the model and API guide](MODELS.md) for exact model IDs, supported speed modes,
context budgets, private host key overrides and provider-specific limits.

## Skills and folder references

Type `/features` in a general message to open the skills menu above the composer.
Continue typing to filter the available recipe names and descriptions. The catalog
comes from the harness's actual `recipes/*/SKILL.md` files, currently Clip, Remake
and Shorts. Select by clicking, or use Up/Down then Enter or Tab. Enter selects a
menu item without sending the message; Escape dismisses the menu. Shift+Enter still
inserts a newline. Selections appear as removable chips.

Type `@` at the start of a word to choose a folder in the current conversation's
`work/` workspace. Folder paths filter as you type; email addresses are ordinary
text. Folder names are indexed on demand when a conversation or picker opens, up
to 500 directories and eight levels. Hidden folders, dependency/cache folders and
symlinks are excluded. This is a directory-name index of the agent's readable
workspace, not a semantic index of the entire repository. A new conversation can
select skills; folders become available after its workspace exists.

Choose up to 12 references across skills, folders and plugins, then write your message. Each conversation keeps its
own unsent reference selections while switching between chats. These drafts are
in browser memory and are lost on page reload. Successful submission removes only
the references sent; additions made during a pending save remain. Question answers
do not consume selections staged for the next general message.

Selected references travel as structured context in the durable queue and saved
human chat turn. The agent receives skill names with `skill_read` instructions and
folder paths with `file_list`/`file_read` instructions. The human message text stays
unchanged; references appear alongside the message and queued entries. The backend
validates selections both at enqueue and execution. A folder removed while queued
fails explicitly before starting an agent turn. Selections do not grant new access
or automatically attach all folder contents.

API: `GET /api/features` lists skills; `GET /api/requests/:id/features` also lists
folders. General message/new-request payloads accept
`context: {skills: ["remake"], folders: ["planning"]}`. Queue workers forward this
context to the orchestrator, and host recovery includes saved selections.

Verification: 134 Python tests pass, including reference validation, queue restart,
HTTP submission and agent prompt binding. DOM interaction tests use the real Eeva
bundle and cover filtering, keyboard/pointer selection, Escape, removal, conversation
drafts and asynchronous submission. A fresh browser test tab was blocked with
`ERR_BLOCKED_BY_CLIENT`; rendered visual layout remains unverified. No paid model
turn was started for this feature's tests.

## Workspace canvas

The activity rail switches between Conversations and Workspace files. Click the
active rail control to hide its navigator. Drag the divider at the navigator's
right edge to resize the workspace sidebar, or the divider between chat and canvas
to adjust their widths. Both support arrow keys, Home/End and double-click to reset;
widths survive reloads. Chat remains on the left with a collapse control. Split
view remains available from 768px; smaller windows use the Chat/Canvas switch.
The canvas holds Outputs,
Browser, Image studio, Video studio, and file tabs. Opening the same file focuses its existing tab. Split the
canvas into two groups to compare files, move the active tab across groups, or join
the groups without discarding tabs. Layout and open tabs are kept per conversation
while the page is open; reloading starts with Outputs.

The explorer lazily lists public request folders. Expand folders, filter visible
loaded filenames, use Load more for paginated directories, and Refresh workspace
files after external edits. It does not expose private browser, memory, credential
or runtime files. The bounded text preview preserves literal text and offers the
original file when truncated. Video/audio use native controls; images fit the canvas.
HTML runs with scripts in an opaque sandbox, without same-origin access to the
workbench; relative files use a request-scoped preview capability. PDF pages are
rendered by the host with bounded dimensions and previous/next page controls,
avoiding dependence on the browser's native PDF viewer. The original file remains
available through Open.

New integration imports open the first image and response JSON in the current
conversation's canvas. Historical import events never reopen tabs on page load;
seen sequence tracking prevents polling from reopening a manually closed tab.
Importing files and opening them do not dispatch another agent turn.

## Image and video studios

Open **Image studio** or **Video studio** from the canvas's new-tab menu or the
workspace navigator. Each studio belongs to the current conversation. Its jobs run
independently of the chat queue, so generation does not start another agent turn or
block typing in chat. Recent generations shows durable job history. Open a finished
result in a file tab, download it, edit it again, or reuse its settings and saved
inputs. Generation details exposes tool warnings and quality notes, including
seam or extraction-fidelity concerns. Draft controls and drawings also survive page reload in session storage
when the browser permits it; submitted jobs are saved on the host.

Drop images or videos from your computer, drag a file from the current workspace
explorer, or enter a public workspace file path. Images accept PNG, JPEG and WebP
up to 40 MiB and 16 megapixels, with an 8192-pixel edge limit; they are decoded,
orientation-corrected and normalized to PNG. Videos accept MP4, MOV, M4V, WebM and
MKV up to 250 MiB. Video edit/extend sources must be at most ten seconds. Inputs
are snapshotted before submission, and uploaded/registered assets are checked
against their saved hashes before their provenance is reused. Cross-request drag
references, private paths and symlinks are rejected.

Image generation defaults to Sunburst; background removal defaults to local
source-preserving masking. Width and height are normalized to the
adapter's size rules: multiples of 16, aspect ratios from 1:3 to 3:1, a maximum
3840-pixel edge, and 655,360 to 8,294,400 pixels. The canvas displays the effective
size. Effort selects low, medium, high, extra high or max. Creation and
reference-guided edits expose Transparent background and Allow text controls.

- **Create** generates from a prompt and optional references.
- **Edit** generates a new image guided by its source and references. Optional
  markup is sent as an additional annotated reference with instructions to follow
  the marks without reproducing them. It does not guarantee unchanged pixels.
- **Inpaint** uses a painted PNG alpha mask at the source's exact dimensions.
  Alpha above 127 selects editable pixels; transparent pixels remain unchanged.
  The studio calls `sunburst_inpaint` with `exact_mask=true`, which feathers inward
  and restores every unselected source pixel. Source dimensions and the original
  alpha channel are retained. The existing agent-tool default can still expand its
  feathered mask unless `exact_mask` is requested.
- **Outpaint** lets you move and scale the source within a larger canvas. The
  model fills the surrounding area. Placed source pixels are restored except for
  a 32-pixel blend along edges adjoining generated pixels; the receipt identifies
  the retained and exact boxes.
- **Remove background** defaults to **Preserve source (local)**, which needs no
  provider credential. Draw a subject rectangle or use the whole image. Existing
  source alpha is retained; otherwise a flat-background key or GrabCut estimates
  the foreground. Original RGB values are kept. The cutout and a separate
  full-source-size `background-mask.png` are saved with segmented provenance.
  The mask's alpha is `255 - foreground alpha`: opaque pixels select background
  for editing, including areas outside the chosen subject region. **Use background
  mask** loads the pinned original source and mask into Inpaint. Local segmentation
  is approximate, so inspect the mask first; GrabCut uses an inset seed to retain
  background samples when the whole frame is selected. Inpaint still needs a
  configured image provider and retains the source's original alpha channel.
  **Regenerate cutout (Sunburst)** instead requires a subject description and may
  redraw it. Fidelity and text comparisons remain in its tool result and receipt.
  No source-aligned mask is inferred from the model's bounding-box registration.

Video defaults to Omni 1.1. Choose a prompt, first and optional last frames, up to
three image references, or a video to edit/extend. Controls expose 3–10 seconds,
720p or 1080p, and 9:16 or 16:9. Generated clips retain their provider audio. The
model picker comes from compatible configurations in `config/slots.yaml`: the
current image catalog includes Sunburst and its configured Flare alternative;
the current video catalog has only Omni 1.1. Adding a compatible configured model
enables a real choice without claiming support for a different vendor's protocol.
The selected model stays pinned for that job, including retries. Provider keys
remain in the host gateway and are managed through Models and APIs.

Uploads are saved under `work/studio/inputs/<upload-id>/`. Job inputs, outputs and
`receipt.json` are kept under `work/studio/images/<job-id>/` or
`work/studio/videos/<job-id>/`. Generated files also have immutable `slot:…@…`
asset references with provenance, hashes and measured media metadata. These paths
and receipts are available to subsequent agent work. Generation does not commit a
composition or accept a finished video.

Private job records live in `.studio/jobs/`; canonical staging and upload receipts
also stay in `.studio/`, outside the agent's mounted work folder and public file
explorer. The server permits two active studio workers and up to twelve owned jobs.
Duplicate submission keys return the original job rather than paying twice. Stop
job terminates the owned local process group. A parent-loss pipe and a one-hour
deadline stop orphaned or hung local workers. Graceful shutdown cancels outstanding
jobs; unfinished records found after an abnormal restart become Interrupted.
Nothing automatically replays or resumes a generation. Stopping the local worker
cannot guarantee cancellation or refund of a request already accepted by a provider.

API routes are request-scoped under `/api/requests/:id/studio`: GET
`/capabilities`, GET/POST `/jobs`, GET `/jobs/:job-id`, POST
`/jobs/:job-id/cancel`, and POST `/uploads` for binary media. Mutations use the
same loopback Host, Origin and CSRF checks as chat. Job responses include reusable
input/mask metadata and capability-scoped preview URLs; no provider credentials
are returned. Background jobs accept `background_method: "matte" | "sunburst"`
(default `matte`). A local result's `background_mask` holds its separate mask
metadata and preview URL; `reusable_inputs[0]` is the pinned source for subsequent
inpaint. The main `artifacts` list continues to hold the cutout. Generation runs
through the existing `Registry.call` policy, role
and call-log path. Public file writes use descriptor-pinned, no-follow publication.

Backend verification covers upload/asset tampering, mask geometry and exact
outside-mask equality, private paths and symlinks, idempotency, history/reuse,
policy enforcement, cancellation, parent loss, deadlines and HTTP origin/CSRF
checks without paid calls. Local mask tests also cover credential-free execution,
off-origin region reconstruction, safe whole-frame GrabCut initialization, original
RGB preservation and edge-touching alpha, including partially transparent pixels.
Separate live provider checks produced a 1024×1024
Sunburst PNG and a three-second 720p Omni clip with 72 decoded frames. These checks
confirm those provider paths; they do not qualify every model, effort or operation.

## Plugins and MCP connections

The Plugins dropup beside the model pill lists the server's actual integration
catalog and status. Select a plugin to stage it for your next general message;
selection does not connect it or grant access. The same per-conversation draft
rules apply as for skills and folders. Unavailable integrations stay selectable so
the agent can explain setup, but their tools cannot run until the host verifies them.

Manage connections opens public gateway/client/callback/read-tool settings. The
callback defaults to this server's exact local URL. Connect Solstice opens a user
sign-in popup; the host owns OAuth state, PKCE and tokens. Figma's separate Connect
Figma action appears only when the authenticated gateway advertises a configured
Figma extension and the user grant is missing. Popup completion triggers a fresh
tool check, bounded to five minutes with a manual Check tools fallback. Disconnect
also clears stale local connection state. No tokens or client secrets are entered
into or returned to the frontend.

## Conversation browser

Choose **Browser** in the header or explorer to open its canvas tab. Native browser
tabs stay inside it. Resize or collapse chat to give the browser more room, or
place another file in a second canvas group. At narrow widths, the Chat and Canvas
controls switch views. Closing the canvas tab stops viewing but preserves the
browser session; **Close browser** ends the session.

Open browser defaults to headed mode, which opens a visible Chrome window and
shows its live image here. Select headless before starting if no separate window
is wanted. Close the browser before changing modes. Remote browser is also available when the host explicitly configures a remote
endpoint. Existing remote sessions are labeled Remote; connection details stay
on the server.

The agent owns a newly opened browser. Take control enables the address field,
back/forward/reload, tabs and direct interaction with the live page. Return to agent
releases held keys/buttons and hands control back. Navigation and keyboard/pointer
input share a serialized queue; frame acknowledgements travel independently.
Pointer moves and wheel events coalesce without reordering button or key events.
Click the page to type; Escape returns keyboard focus to the ownership control.
The viewport uses a text input sink for ordinary text, paste and composition.
Select-all uses a bounded native compatibility action for text inputs and editable
content, including same-origin frames. Cross-origin frame select-all is currently
unsupported; ordinary pointer interaction and text input still use the native
browser.

The Solstice mark shows the agent's verified cursor position during agent control.
Image pixels, viewport CSS coordinates and letterboxing are accounted for in both
cursor placement and human input. Unknown coordinates do not produce a cursor.
Page alert/confirm/prompt messages appear as plain text inside the panel, with
Dismiss/Accept and a response field for prompts. They require human control.

Connection errors pause input and expose Reconnect. The viewer opens an SSE stream
only while the panel is open and the session is running. Each decoded frame is
acknowledged after a paint, including identical images, so the native driver keeps
at most one frame in flight. Frames from a prior session/tab are acknowledged and
discarded. No native WebSocket/CDP address or provider token reaches the frontend.

The original ten-short baseline conversation is registered through
`runs/.ui/history.json`. The registration points to its existing request within
`runs/benchmarks/`; assets, chat bytes and native thread remain in place. This is
an explicit request registration, not recursive discovery of all benchmark runs.

## Questions

Questions appear above the composer. Previous/next controls switch between them,
and each question retains its own selection and typed draft. A selection is sent
only when you press Send. Free text is supported without selecting an option.
The answer is recorded through the existing broker and sent as a conversation
message. Duplicate submissions are rejected before another turn is queued.

While a question is expanded, the composer answers that question. Collapse it to
write an unrelated message. Attachments are kept for that general message. Old
defaulted questions are labeled as earlier choices you can review; viewing them
does not apply another timeout or accept the recommendation.

## Activity and context

Thinking expands to the public reasoning summaries and messages emitted by the
provider. It does not expose hidden chain of thought. Old runs may have no summaries
because they requested none. New Codex turns request automatic reasoning summaries.
Tool cards expand to input and output, including bash commands when available.
The frontend polls incremental events, and periodically refreshes request/output
state. Scrolling up keeps your place; Jump to latest returns to the active turn.

The small pie in the composer footer opens context usage. Codex uses its reported
token count and context window, configured to the selected budget. Direct Anthropic
and OpenRouter use measured input plus output from the last call, divided by the
selected working budget. The conservative next-call estimate remains backend-only.
Cached input is part of input, and reasoning output is part of output. Conversation
totals are separate cumulative gateway usage, including helpers. Missing context
measurements are shown as not reported.

Compact conversation uses Codex's native `thread/compact/start` on the existing
thread. It is available while idle, and may call the model. Conversation history,
workspace files and decisions remain available; the model's earlier context is
summarized. A protocol acknowledgement alone is not treated as successful compaction.

## Verification and current limits

Backend tests cover serial queues, cancellation, Send now ordering, restart recovery,
question races/duplicates, source uploads, origin/CSRF protection, path confinement
and byte-range video playback. Harness tests cover emitted activity, context versus
cumulative accounting, native compaction completion/failure and interrupted thread
persistence. DOM interaction checks exercise the real Eeva bundle with actual saved
conversation data and mocked mutations, without starting a paid model run.

The earlier controlled preview blocked local URLs. The browser integration was
subsequently tested against the real local server at desktop and mobile widths
(1280×720, 1024×600, 640×560 and 390×600), including the saved ten-short chat,
resizing, ownership, live images and direct page input. Live checks verified
new/select/close tabs, navigation history, prompt responses, wheel scrolling,
Escape focus, Meta/Control select-all with long text, and the agent mark at exact
native coordinates. Pure coordinate/input
checks cover letterboxing, captured drag bounds, URL validation and event order.
These checks do not revalidate every older chat interaction.
Native compaction's protocol was checked against the installed Docker image and
its lifecycle tested with fixtures; a paid live compaction and a 60-minute silent
stream were not run. Nothing in this frontend changes output acceptance or the
existing verification flags.

Workspace canvas checks use an isolated disposable request, without paid model
calls or changing the ten-short request. Behavioral frontend tests cover immutable
canvas transitions, global file-tab deduplication, pagination merge, historical
import suppression, plugin context and public configuration serialization. Live
checks cover 260-file pagination, HTML relative CSS/images/classic and module
scripts plus parent-access isolation, image/text split groups, narrow layouts,
truthful disconnected catalog states and import tab closure across reload.

The workspace update also passed live embedded-browser startup, live viewing,
human ownership and session close. Native PDFs were blank in the in-app browser;
the bounded bitmap renderer resolved that limitation and passed one- and two-page
navigation checks. A real 640×360 video fixture loaded with its measured one-second
duration. The disposable request was removed afterward; the completed ten-short
conversation and its source/native-thread records were unchanged. OAuth grant
completion was not exercised against a real account; the UI truthfully showed
Solstice Needs setup and Figma Unavailable, with no connected state fabricated.

The studio build passed 507 regression tests on 2026-10-14. Live checks in a
separate synthetic request verified a real 1024×1024 Sunburst generation, painted
inpainting, 1024×1536 outpainting with custom placement, local background removal,
mask refinement followed by Sunburst background replacement, and a real three-second
720p Omni clip. Both inpainting paths preserve every unmarked pixel. File upload,
workspace drag-and-drop from Quick Open, draft recovery after reload, tab switching,
video playback and 390px/800px responsive layouts were checked in the browser.
The Solstice mark and wave-of-dots waiting state rendered during generation.
The original ten-short request was not used for these checks. Other video modes
and alternate configured models are covered by adapter/contract tests, not by
additional paid provider runs in this verification.
