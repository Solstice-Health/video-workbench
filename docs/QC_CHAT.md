# QC chat: pinned findings and fresh-context rechecks

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
`needs_recheck`. This invalidation also happens when QC is opened after a render
created outside the UI. It does not start a model call or mark the new render as
passing.

## Compare and recheck a finding

For an open finding, choose a newer rendered version of the same output and
select its frame window. Compare the original and newer windows before submitting
**Recheck with fresh context**. Different frame rates or durations require an
explicit new range. Equal clocks are only a convenient initial selection: cuts,
retiming and composition changes can still move the subject. Region overlays
reuse normalized coordinates; they do not track objects. Paired seeking aligns
elapsed time from the chosen window starts, not semantic content.

The recheck joins the QC queue and uses its existing Stop/Resume controls. Every
attempt, including a resumed attempt, starts with a new provider history. It sees
only the frozen finding's expectation/observation/category, the original window
and the selected newer window. It cannot read either continuing chat, other
findings, attachments, workspace files or the editor's reasoning. It can inspect
only the two selected windows and submit one structured assessment. Known author
model families must differ from the reviewer. Unknown historical authors remain
unknown.

The result is **Issue observed**, **Not observed in sampled frames**, or
**Inconclusive**. The host records exact inspected frames and media hashes from
successful inspection calls. A substantive report requires images from both
windows to have reached a previous model request; a report issued alongside its
first inspection calls is refused. Each attempt permits at most 12 model
responses, 8,192 output tokens per call (including truncation retries), and 64
inspections. Reports are persisted before acknowledgement and finish the attempt
without another generation. The result remains an **unqualified visual review**:
these checks have not been calibrated on held-out defects, and sampled images
cannot establish whole-video, motion or audio correctness.

Rechecks never resolve or dismiss findings. A later human disposition, changed
finding revision or newer render makes the recorded result stale without erasing
it. Changed media bytes prevent completion. Failed attempts retain their private
logs; an explicit retry starts another fresh attempt. Restart does not replay
queued work. No automatic paid recheck is scheduled by polling or rendering.

QC timeline markers refer to findings on the exact selected media pin. Edit's
output panel receives only the frozen findings explicitly sent by the person;
private observations, rechecks and later dispositions remain private. A finding
from an older output version is labeled accordingly rather than seeking an
unrelated frame in the current version.

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
and recheck invalidation, preparation and completion. Version pins, actor, model and timestamps remain attached
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
| `qc_recheck_report` | `qc.recheck` | Fresh reviewer only; host-recorded coverage, no human disposition |

Inspection receipts record the exact frames examined. Samples do not establish
coverage of unsampled frames or a whole-video pass. Checklists can be read with
the existing attachment and PDF tools inside the private QC workspace.

## Next increments

1. Qualify review checks against known-good examples and planted defects; evaluate
   bounded automatic rechecks before adding explicit opt-in scheduling.
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
HTTP authorization and frontend geometry/state transitions. They also cover
encoding failures, evidence delivery, durable report acknowledgement, hard output
limits, cached pin validation, and multi-digit comparison frame entry.

The recheck increment passed **2,114 tests, with 9 skipped**. Browser checks covered
paired frame seeking, pinned timeline selection, explicit queue submission and
editor handoff filtering. Two live synthetic cases used generated red rectangles:
the corrected clipping was reported as not observed in sampled frames, and the
unchanged clipping as persisting. Both findings retained their human-review
status. These small smoke checks do not establish model quality or qualification;
the regression suite otherwise stubs provider calls.
