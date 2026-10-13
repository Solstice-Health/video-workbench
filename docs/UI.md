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
attach a source video, and describe the edit. The output panel shows the latest
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

Choose up to 12 references, then write your message. Each conversation keeps its
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

The controlled Chrome preview blocked local URLs with `ERR_BLOCKED_BY_CLIENT`, so
desktop/mobile visual layout and real-browser interaction QA remain unverified.
Native compaction's protocol was checked against the installed Docker image and
its lifecycle tested with fixtures; a paid live compaction and a 60-minute silent
stream were not run. Nothing in this frontend changes output acceptance or the
existing verification flags.
