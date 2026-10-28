# Projects, source intake and conversation history

## Working with downloaded and attached videos

Select a downloaded video with `@` or drag its workspace file into the composer.
The next turn registers an immutable copy as source media before the lead runs.
All selected or attached videos are registered together, with digest deduplication;
there is no one-video-per-message limit. A changed file creates a new source.

The agent can use `media_register(path)` for a file already in its working folder.
It returns a source reference; `index_build(media_id)` indexes that source when
needed. Source-specific inspection and indexing use `media_id` rather than
silently substituting the first video. Registering a file never hard-links a
writable download into immutable source media.

## Questions and steering

The question card has separate **Answer question** and **New instruction** modes.
Each retains its own draft. General instructions keep their attachments and
references when the person switches modes. A general instruction is never taken
as approval of an open permission question.

For a live blocking question, submitting an answer delivers it to the waiting
tool immediately; it does not enqueue a duplicate model turn. A nonblocking
answer can be sent now by stopping the current turn and continuing with the
answer, or queued. General instructions have the same Send now / queue choice.
The workbench waits for interrupted workers to exit before starting the next turn.

## Projects

The conversation sidebar lists projects and conversations without a project.
Create a project, create a conversation inside it, or move the selected
conversation to a new or existing project. Project membership is stored locally.

Conversations in a project use one real shared `work/` directory, including files
opened in the canvas, downloads, imports, code, studio outputs and working
canvases. Their request records, original attachment stores, provider histories,
questions, QC conversations and committed output versions remain separate.
Working copies of attached files are part of the shared working directory.
Media and asset references must be registered in the conversation using them.

Joining from a standalone conversation copies its working files into the project
without overwriting different files. A path conflict leaves membership unchanged
and identifies the file to rename; a new project is another option. Moving from
one project to another selects the destination's workspace and leaves the old
project's files in place. No directory symlink grants access to private state.

Editor turns serialize across a project to avoid concurrent writes to shared
working files. Each conversation keeps its own queue. Send now never interrupts
a different conversation in the project. Independent QC conversations can run
alongside editing. Stop active work and remove queued messages before moving a
conversation or changing its history.

## Fork, rewind and edit

Saved human messages expose **Fork from here**, **Rewind to here** and **Edit
message**. Assistant replies expose fork and rewind. Operations use stable
message identities, a history revision check and retry keys.

- Fork copies the conversation through the chosen message. A project fork uses
  the same shared working directory; a standalone fork copies current working
  files and required media. It starts with fresh provider context.
- Rewind keeps the chosen message and removes later messages from the active
  history. The previous request and provider state are retained in a private
  recovery backup. Old jobs cannot be resumed into the rewound history.
- Edit + continue keeps the history before the selected human message, then
  sends the edit with that message's original attachment and reference state.
- Edit + new conversation performs the same operation in a fork, preserving the
  original conversation.

These are conversation checkpoints. Working files and committed outputs remain
at their current state; there is no filesystem rollback or tool-call checkpoint.
The UI states this before the operation. Fresh provider sessions replay retained
messages and attachment references; later native sessions, notes, asks and model
history are quarantined. The signed ledger is preserved, with a sequence fence
that prevents removed future human events from re-entering the model context.

Recovery material is under `<conversation>/.conversation-history/`. Keep it with
the workspace backup. It is excluded from agent work mounts and public previews.
Retrying an uncertain operation uses its existing operation ID and returns the
same fork or edit dispatch rather than repeating the mutation.

## Validation

Regression tests cover multiple video inputs, source selection, question delivery,
project queues and path mapping, file-transfer snapshots, stale history revisions,
interrupted-operation recovery, provider context resets, and preserved attachment
bytes. Browser tests use synthetic conversations and a local fixture worker;
they exercise the product routes without making paid model calls.
