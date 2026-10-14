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
Browser, and file tabs. Opening the same file focuses its existing tab. Split the
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
