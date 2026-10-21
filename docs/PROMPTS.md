# Main-agent prompt profiles

Open the model picker below the composer, choose **Agent prompt**, or open **Models and
APIs → Agent prompt**. Choose a profile, inspect its full effective prompt and
source links, then save it for this conversation. Before opening a conversation,
the same control changes the default for new conversations.

The six profiles are:

- **Descriptive**: the existing detailed Workbench capabilities and editing workflow.
- **Minimal**: the common harness contract plus a short instruction to inspect, act and verify.
- **Loose**: outcome-focused work with more discretion over workflow and delegation.
- **Cursor inspired**: targeted workspace context, focused edits, concise progress and task completion.
- **Claude Code inspired**: adaptable exploration, action and verification, with context-aware delegation.
- **Codex inspired**: persistent implementation, appropriate verification, evidence and clear ownership.

The last three are Workbench adaptations of public guidance, not copies or claims
about the exact private system prompts in current vendor products. Public vendor
guidance favors smaller prompts, selective context and model judgment rather than
repeating a detailed itinerary. Sources are linked in each profile's preview. No
profile forces the agent to spawn helpers; the agent can choose independent work
that benefits from delegation.

Every profile includes the same shared contract: role and tool boundaries,
untrusted content handling, human authority, provenance, source-faithfulness,
verification and honest delivery. Selecting a style never grants extra tools,
changes worker prompts, changes policy, or accepts an output.

## Selection and saved turns

Conversation choices live in `memory/prompt_selection.json`. The default is stored
in `runs/.ui/prompt_preferences.json`; Descriptive is used when no choice exists.
New conversations capture the current default as their own choice.

Every submitted message captures the complete assembled prompt, profile ID,
label and SHA-256 in `main_config.prompt`. Active turns and existing queued
messages keep that snapshot when preferences or profile files change. Model
selection is independent. The original slot and fallback chain remain intact
when only a prompt is selected.

**Resume turn** uses the current UI model and prompt selections for the new
continuation. The interrupted job and any messages already queued retain their
original snapshots. The API can independently retain or replace the model and
prompt using `use_current_model` and `use_current_prompt`; both default to false.
Jobs created before profile support retain Descriptive behavior when resumed
without an explicit override.

Persistent native threads remain the same conversation when a profile changes.
The next turn receives replacement instructions containing its exact captured
prompt. Provider API hosts receive the captured prompt as their system message.
The harness records the actual prompt fingerprint with each host thread so a
change is delivered once, rather than inferred from the selected profile's name.

Anthropic binds signed reasoning to the system prompt and tool definitions that
produced it. When that prefix changes, the harness leaves earlier reasoning out
of the outbound request once, then preserves new reasoning normally. The original
reasoning remains archived alongside the complete conversation and completed tool
results. A legacy conversation whose prefix was never recorded gets the same
one-time reset. This prevents invalid-signature errors without repeatedly dropping
new reasoning or silently rewriting the saved history.

## HTTP interface

- `GET /api/prompts` returns all six profiles, source links, full effective text,
  hashes and `default_profile_id`.
- `POST /api/prompts/preferences` accepts `{"default_profile_id":"minimal"}`.
- `GET /api/requests/<id>/prompt` returns `profile_id`, its preview and the active
  turn's captured profile metadata when available.
- `POST /api/requests/<id>/prompt` accepts `{"profile_id":"loose"}`.
- `POST /api/requests/<id>/resume` can include `job_id`, `use_current_model` and
  `use_current_prompt`.

Mutations use the normal same-origin and CSRF protections. Arbitrary prompt paths
and chat-supplied system prompt text are not accepted. The implementation lives
in `src/vwb/prompts.py`, `agents/prompts/main_*.md`, `agents/job_lead.py` and the
existing queue and model controls. Worker roles retain their separate prompts.
