# QC chat: first build increment

The QC feature starts from `codex/feat-code-intelligence` at `b974eee`, on
`codex/feat-qc-chat`. The first increment provides a usable independent review
conversation, pinned findings and a deliberate handoff to the editor.

## Review workflow

Open an existing conversation and choose **QC** above the chat. Edit and QC keep
their own histories and drafts, while the existing workspace canvas remains
available. QC has its own send queue, Stop and Resume actions. Both conversations
can run concurrently; each serializes its own turns.

Launch with `uv run vwb ui` and use the browser it opens, or its printed launch
link. That link grants a private, HttpOnly browser-session cookie for QC routes;
the ordinary bootstrap endpoint does not reveal the credential. Opening the bare
localhost address in another browser does not grant access to QC history. A
server restart requires the new launch link.

Choose a rendered output version in the QC review player. Select one frame or a
half-open frame range `[f0, f1)`, optionally draw a rectangle, then describe what
was expected and what was observed. Coordinates are normalized to the video
image, excluding letterboxing. Findings retain the exact canvas, render and
referenced-source hashes. The media pin submitted by the viewer must still match
when the finding is saved. Priority expresses a review recommendation; it does
not change verifier flags or release acceptance.

The QC conversation accepts text, images and checklists as private attachments.
The reviewer can inspect frames and bounded frame sequences and create findings.
It cannot edit files, generate media, launch workers, run scripts, change external
applications, send findings to the editor, or resolve them.

Select shared findings and choose **Send to editor**. The editor receives a frozen
batch of typed findings, including the version, frames, region, expected result
and observation. Retrying an uncertain submission uses the same queue key.
Audit findings cannot be sent through this route. Later private notes and status
changes do not silently alter an already sent batch.

Human resolution and dismissal are explicit events with revision checks. When
an editing turn produces a newer rendered version, unresolved findings become
`needs_recheck`. This first increment records the need for a recheck; it does not
run an autonomous second judge or mark the new render as passing.

## Context and persistence

The parent request remains the editing conversation. QC has a separate workspace
at `.qc-chat/runtime/`, including its request record, provider histories, queue,
turn lock, attachments, model activity, usage and tool logs. The queue identity is
`qc:<parent-id>`, which cannot collide with a valid parent request identifier.
Inspection handlers receive a trusted reference to the parent and only admit
versions captured when the QC message was queued. General editor file tools and
the shared workspace file explorer do not expose this private directory.

`qc_lead` uses the existing persistent Anthropic/OpenRouter API host with an explicit
tool allowlist. It has no shell or agent-delegation tool. Its model is configured
independently in `config/slots.yaml` under `qc`; the initial slot uses Anthropic.
The queue freezes the slot, instructions and media snapshot for each submission.
Resume retains that snapshot and completed tool effects. Changing the configured
model carries forward only the QC public conversation, never the editor's chat.

New canvas commits capture the active author's model in provenance. Inspection by
the same known model family is refused. Older versions may lack this provenance;
their author model remains unknown, so model-family independence is not claimed.
Separate history and tool authority still apply. Provider credentials are managed
through the existing host configuration; no live model call is made by opening QC.

This is an interactive review boundary, not the separately specified sealed
evaluation harness. The person can see both conversations. A full sealed judge
requires its own bounded evidence package and held-out evaluation process.
Explicitly enabled native application scripting runs with the desktop user's
privileges; it is not a filesystem isolation boundary for a sealed evaluation.

## Findings and learning evidence

`QCStore` stores `.qc-chat/items.json` and an append-only `.qc-chat/events.jsonl`.
The log is authoritative and repairs a stale materialized snapshot after a crash.
Events distinguish human findings, model observations, human disposition, handoff
and recheck invalidation. Version pins, actor, model and timestamps remain attached
to the evidence. A machine observation is never silently relabeled as human gold.

These records are the foundation for the RSI work: collect attributable review
examples first, then split by project into development, promotion and held-out
sets. No prompt or check self-modification runs in production in this increment.

## Inspection tools

All tools use `Registry.call` and the policy dial:

| Tool | Capability | Evidence |
|---|---|---|
| `qc_targets` | `qc.inspect` | Frozen rendered version metadata |
| `qc_view` | `qc.inspect` | One full frame, optional crop and coordinate mapping |
| `qc_frames` | `qc.inspect` | 2–12 numbered sample frames in an explicit range |
| `qc_items_read` | `qc.read` | QC sees its findings; other roles see only human-sent snapshots |
| `qc_item_create` | `qc.findings` | Model observation, immutable media pin and actor/model attribution |

Inspection receipts record the exact frames examined. Samples do not establish
coverage of unsampled frames or a whole-video pass. Checklists can be read with
the existing attachment and PDF tools inside the private QC workspace.

## Next increments

1. Fresh-context recheck execution with explicit coverage and qualified checks;
   version comparisons and timeline markers in both views.
2. A larger review board with multiple synchronized media lanes, source/imported
   videos, arrows and freehand marks, and tracked regions.
3. Qualified paired-frame/window questions, detection and segmentation adapters,
   measured Figma fidelity and deterministic image/audio checks.
4. Script qualification against known-good examples and planted defects, shadow
   playbooks, held-out judge runs, canaries and offline promotion experiments.

## Validation

Tests cover pin integrity, revision conflicts, private finding filtering,
interrupted handoff recovery, concurrent writes, new-version invalidation,
provider history isolation, forbidden tool calls, queue concurrency and restart,
HTTP authorization and frontend geometry/state transitions. Model tests stub the
provider boundary; they do not establish live model quality or qualification.
