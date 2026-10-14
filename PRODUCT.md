# Video Workbench

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The user edits and reviews videos through a continuing conversation with one main
agent. The repository currently targets pharma video. Broader user roles and
multi-user collaboration have not been specified.

## Product Purpose

Make editable video outputs from source material, accept follow-up direction,
show the agent's available activity, and keep unresolved decisions and output
review visible. The requested frontend brings the existing CLI workflow into
a professional chat workbench.

## Operating Context

The existing Python harness runs locally. Each request has source media, an edit
index, compositions, rendered versions, verification evidence, questions, and one
continuing agent conversation. Agents can choose to start helpers. The control
plane owns tools, provenance, checks, flags, and human acceptance.

## Capabilities and Constraints

- Show provider-emitted reasoning summaries and compact, expandable tool calls.
- Allow queued messages and explicit interrupt-then-resume steering.
- Present nonblocking questions with options, navigation, and typed answers.
- Show last-reported context usage separately from cumulative token usage.
- Offer native compaction without replacing the conversation thread.
- Browse conversation files in an activity-rail explorer and open images, video,
  HTML, PDF pages and text in closeable canvas tabs, with optional split groups.
- Collapse or resize the left chat without losing its conversation or draft.
- Select real plugins/MCP integrations for each message and manage their connection.
- Show a conversation-scoped browser inside the canvas, with live viewing and explicit
  human/agent ownership. Headed mode is the default; headless is optional.
- Keep the completed ten-short baseline conversation discoverable through a
  durable registration, with its existing native thread and media in place.
- Preserve source provenance, output versions, verification and acceptance rules.
- Keep vendor keys in the host gateway, away from the browser and agent plane.

## Brand Commitments

Use the organization's Eeva design system. The user requested a sleek,
professional chat interface, with no decorative accent bars or lines.

## Evidence on Hand

Existing request workspaces and rendered videos are under ignored `runs/`.
Architecture is documented in `docs/ARCHITECTURE.md`. Eeva's original tokens,
component source, fonts, and provenance are vendored with the frontend.
No output, token count, or successful check should be invented for presentation.

## Implementation Assumptions

This version is a local single-user interface for the existing harness. A left
activity rail switches between conversations and workspace files. Chat sits to the
left of a large canvas, with closeable tabs and up to two groups. Outputs open by
default. At narrow widths, Chat and Canvas switch between full-width views.
Integration selections express intent; only an authenticated, verified tool catalog
is presented as connected. Figma requires a deployed gateway extension and its own
user grant. No connection or output is simulated in the application.
