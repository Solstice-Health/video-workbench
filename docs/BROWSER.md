# Visible browser workspace

Decision and source review: **2026-10-14**. The local dependency is pinned to
**agent-browser 0.39.1**, under Apache-2.0. This document distinguishes the chosen
integration contract from hosted capabilities that have only been researched.
The Python integration, independent contract tests, native core acceptance, continuous
stream checks and rendered frontend acceptance pass. Hosted deployment remains untested.

## Decision

Run an isolated, headed Chrome browser locally by default, controlled by the native
agent-browser CLI and daemon. Use the same toolkit in optional headless mode. The
existing Video Workbench agent owns planning and the browser loop; a second hosted
agent does not take over the task. Remote CDP configuration can attach the driver to
an externally managed browser on a heavier server.

The upstream native runtime does not require Node or Playwright. This fits the
repository rule that Node belongs only under `generators/remotion/`. The pinned
toolkit supports snapshots with element references, screenshots, stable tab handles,
headed operation and remote CDP. We use those primitives through a constrained
Python service, not its independent chat/dashboard application. See the
[pinned README](https://github.com/vercel-labs/agent-browser/blob/v0.39.1/README.md),
[release](https://github.com/vercel-labs/agent-browser/releases/tag/v0.39.1) and
[license](https://github.com/vercel-labs/agent-browser/blob/v0.39.1/LICENSE).

The local browser profile is separate from the person's daily browser. Local process
and profile separation is not a VM security boundary. Browser execution is a
control-plane capability, with the same role, policy and logging requirements as
the rest of the toolkit. Indexing work remains separate from this feature.

## Integration contract

| Concern | Required behavior |
| --- | --- |
| Agent tools | Typed operations pass through `vwb.tools.base.Registry.call`, the role allowlist and the policy dial. Do not expose arbitrary CLI commands, host JavaScript evaluation, vendor credentials or raw browser endpoints as an agent escape hatch. |
| Runtime ownership | Keep the browser runtime private to one request. Track its session generation, process ownership and cleanup state. Separate identities use separate profiles and sessions. |
| Live view | Proxy browser frames and public state through the application's same-origin SSE route, guarded by the existing loopback Host/Origin checks. The frontend does not connect to the native driver's raw WebSocket. The backend controls frame delivery and input authorization. |
| Agent observations | Return bounded DOM/accessibility references plus viewport screenshots when useful. Include an observation identity, session generation, tab identity and viewport metadata. A continuous stream for a person does not require sending every frame to the model. |
| Input ownership | The server owns the human/agent lease. A takeover must stop new agent actions and invalidate queued actions before acknowledging human input. On return to the agent, obtain a fresh observation. Hiding controls in CSS is not authorization. |
| Freshness | Require the applicable observation identity for actions. Check actor, age, document/URL, scroll, viewport, active tab and lease state; coordinate actions also check a DOM mutation revision. Consume an observation before dispatch, including ambiguous failures. This does not prove that every visual target stayed unchanged. |
| Tabs | Address tabs by stable handles, not positions. Synchronize new, closed and selected tabs with the viewer. Make the relationship between the viewed tab and the action target explicit. |
| Cursor | Drive the Solstice cursor from real browser action telemetry, with coordinates and action state. Movement toward a target is not proof that a click succeeded. Hide the agent cursor during human ownership. |
| Coordinates | Keep CSS viewport pixels, screenshot pixels and displayed stream pixels distinct. Account for scaling and letterboxing; do not use full-page screenshot coordinates as viewport coordinates. |
| Reconnect | Reconcile the existing session, tabs, owner and fresh observation before resuming. An uncertain action result must not trigger blind replay of a click or submission. |
| Files | Upload only explicitly authorized named files from `work/`, `media/` or `assets/` through the service. Import supported downloads with size, hash and an untrusted-content notice. The current per-file bound is 128 MiB. A remote browser's file picker cannot access the user's laptop. |
| Shutdown | Hiding a panel or ending a turn worker preserves the session. Graceful UI server shutdown closes UI-managed local daemons after stopping workers; it does not adopt independent CLI sessions merely by reading status. Profiles remain private for reopening. Remote close disconnects the client; the external browser has its own lifecycle owner. |

Browser content, DOM text, screenshots and page-provided tool descriptions are
untrusted observations. They cannot grant permission or change harness policy.
Stream/session credentials stay out of public tool output and durable chat logs.
The local API checks Host, Origin and fetch-site metadata; mutations also require the
existing CSRF token. This is loopback access control, **not per-user authentication**.
A shared or internet-facing deployment must add authenticated identity and request
authorization before exposing this API; request identifiers alone are not authorization.

## Observation choice and limits

Use references for named controls, forms and structured text. Use viewport images
for visual state, overlays, canvas/WebGL content and confirmation of the result.
Screenshot-only control can reach surfaces absent from the accessibility tree, but
loses stable element identity and makes scaling and stale-frame mistakes more
consequential. This is an engineering choice to evaluate, not a claim that a hybrid
approach universally beats visual agents.

The native snapshot path has a **one-level iframe coverage limit** in this integration.
Do not describe it as complete recursive DOM/AX coverage. Nested, dynamic or unusual
frames may need explicit frame targeting, visual interaction or human takeover.
An inaccessible subtree must be disclosed as a coverage limit, not treated as empty
page content. References are scoped to browser/document state, not durable workspace
identities.

An observation expires after 120 seconds. That is an upper age bound, not a guarantee
that a dynamic page has stayed unchanged. The service checks document and viewport
state before an action; a page can still change between validation and dispatch.
Re-observe after meaningful page changes and verify the visible result of consequential
actions. Uncertain action failures consume the old observation rather than replaying it.
A persisted pending-operation record also requires the native command queue to drain
before handoff or another action after an interrupted turn process. Recovery uses a
daemon dialog-status query so an open JavaScript prompt does not block the fence.
An interrupted coordinate click requires close and reopen: its move/down/up sequence
could have left a button held, and releasing it could complete the old click.

The native CLI parses global flags throughout its arguments. The driver permits only
its known internal options in their expected positions; flag-shaped agent text is
rejected before launching a command. This keeps text from changing the session,
configuration or file access, but means such literal text needs human input for now.
See the [pinned argument parser](https://github.com/vercel-labs/agent-browser/blob/v0.39.1/cli/src/flags.rs).

Ctrl/Cmd+A uses a fixed select-all operation because native 0.39.1 omits the Chromium
editing command. It follows focus through same-origin frames and open shadow roots,
and rejects a focused cross-origin frame instead of selecting its parent document.
Input preceding the shortcut is fenced before selection; later input is not dispatched
if selection fails. This helper does not expose arbitrary JavaScript to either caller.
Input connections drain and acknowledge their native frame/event queue so queued frames
cannot starve the delivery pong. Each batch closes its input socket and joins its reader,
including when confirmation fails; uncertain input is not replayed.

Upstream 0.39.1 can return iframe-local bounding boxes without enough information to
translate them reliably. The integration uses unannotated screenshots and suppresses
reference-based cursor telemetry on framed pages. Coordinate actions still report their
actual main-viewport point. This avoids drawing a confident cursor at an unverified target.

Useful primary precedents are [Playwright MCP](https://github.com/microsoft/playwright-mcp),
which exposes accessibility snapshots, scoped search, boxes and optional coordinate
actions, and [BrowserGym's observation implementation](https://github.com/ServiceNow/BrowserGym/blob/main/browsergym/core/src/browsergym/core/observation.py),
which handles frame trees, bounds and focus. BrowserGym's
[environment](https://github.com/ServiceNow/BrowserGym/blob/main/browsergym/core/src/browsergym/core/env.py)
combines screenshots, DOM, AX, tabs and action errors. CDP defines pointer input in
main-frame viewport CSS pixels; see [the input protocol](https://chromedevtools.github.io/devtools-protocol/tot/Input/).
These sources motivate the contract; their benchmark results do not validate ours.

## Hosted and remote options

The external deployments below are **documentation research only**. No hosted provider
was provisioned, paid for or tested for this feature. A separate owned local Chrome
validated CDP attach/disconnect; that is not a test of a hosted provider. The default
remains local.

| Option | Verified fit | Limitation relevant to this product |
| --- | --- | --- |
| **Kernel: preferred hosted headed candidate** | Explicit `headless:false`, CDP endpoint, interactive live view and kiosk mode. Recent images expose read-only toggling and actual-playing events. [Create](https://www.kernel.sh/docs/api-reference/browsers/create-a-browser-session), [viewer](https://www.kernel.sh/docs/browsers/live-view) | Viewer features depend on image capabilities. Read-only presentation must not substitute for our ownership lease. Headless sessions have no live GUI. |
| **Browserbase** | CDP sessions and interactive per-tab embeds; keep-alive is explicit. [Viewer](https://docs.browserbase.com/platform/browser/observability/session-live-view), [sessions](https://docs.browserbase.com/reference/api/create-a-session) | A visible stream does not establish headed rendering. The documented read-only embed uses CSS pointer blocking. |
| **Browserless** | Live URLs offer server-enforced view-only input blocking, tab UI, fixed viewport and reconnect handling; a self-hosted Enterprise path exists. [Viewer](https://docs.browserless.io/baas/monitor-sessions/embedding-live-url) | Stored-credential autofill closes viewers and prevents subsequent viewing for that session. Handoff completion is not proof of task success. [Handoff](https://docs.browserless.io/baas/monitor-sessions/hybrid-automation) |
| **Browser Use Cloud V4** | Standalone browsers expose CDP and live preview, so our own agent can drive them. [Quickstart](https://docs.browser-use.com/cloud/browser/quickstart), [schema](https://docs.browser-use.com/cloud/openapi/v4.json) | Production read-only embeds are explicitly a UI restriction. Disconnecting CDP does not stop the managed browser. [Viewer](https://docs.browser-use.com/cloud/browser/live-preview) |
| **Own heavy browser server** | Keep the same tool/session contract and attach to a privately exposed CDP endpoint. A headed display can also be streamed with a browser VNC client such as [noVNC](https://github.com/novnc/noVNC). | Deployment owns browser isolation, display/stream transport, authentication, network access, lifecycle and resource limits. A CDP URL alone does not implement that infrastructure. |

Stagehand is optional tooling, not a hosting requirement. Current **V4** removed its
`agent()` orchestrator, CUA mode and `highlightCursor`; it leaves the loop to the
application. Do not copy older V2/V3 agent examples into this integration.
[Migration](https://docs.stagehand.dev/v4/migrations/v3)

Remote configuration does **not** create, renew, bill, persist or delete cloud sessions
automatically. Provider provisioning, authenticated endpoint delivery and shutdown
remain deployment responsibilities until a dedicated adapter implements and tests
them. Likewise, mode changes do not promise a seamless live conversion between
headed and headless processes.

Persistent authentication needs deliberate profile ownership. Kernel, for example,
documents whole-profile replacement, not merge, when multiple sessions save, and
requires disposal of pooled per-user state before reuse.
[Profile concurrency](https://www.kernel.sh/docs/browsers/profiles/concurrency)
Remote file handling also needs an explicit bridge; Browserbase documents chooser
interception and transfer through its upload API.
[Uploads](https://docs.browserbase.com/platform/browser/files/uploads)

## Acceptance evidence

The final full repository suite passed **323 tests** on 2026-10-14. Ruff passed
for the changed Python modules, tests and acceptance script.

Independent tests cover the HTTP and service contracts without Chrome or model calls:

- `tests/test_browser_http.py`: 27 passing cases covering same-origin/CSRF checks,
  unknown requests, no browser startup on status reads, safe errors, named SSE events
  and shutdown of UI-managed sessions.
- `tests/test_browser_service.py`: 71 passing cases covering ownership, an in-flight
  handoff, stale observation guards, stable tabs, dead-daemon handling, action allowlists,
  atomic input validation, private status fields, request-scoped frame acknowledgments,
  native-command stream suppression, generation/tab races, partial-start cleanup,
  symlink-safe file transfer, dialog observations, pending-operation recovery and
  select-all ordering/failure behavior for human and agent input, and input receive-queue
  draining with reader cleanup after both success and failure.
- `tests/test_browser_tools.py`: 8 passing cases through the real Registry, covering
  configured role restrictions, image fitting and call hashes, independent policy rows,
  file-policy bypass rejection, artifact hashes and disabled unconfigured remote mode.

These are fake-driver and loopback HTTP checks. Their results alone do not establish
Chrome rendering, native command support, frontend behavior or hosted readiness.

The native service acceptance run on 2026-10-14 passed **19 checks and 35 Registry calls
in each of headed and headless mode**. It exercised form filling, click/select/checkbox,
canvas coordinates, JPEG frames and acknowledgments, stable tabs, file upload/download,
native prompts, human takeover and native input, return to the agent, below-fold content,
first-level iframe targeting, explicit frame selection, disconnect/reopen and cleanup.
It also verified that agent Control+A and human Meta+A delete the full focused input,
including a same-origin iframe input. Both test browsers were closed.
Evidence: `runs/browser-research/service-smoke-20261014-104410-a849f1/result.json`;
reproducer: `scripts/browser_smoke.py`. This run used a local fixture and no model calls.

A separate real **gpt-6-astra** main-agent smoke completed the fixture's form and canvas
task in five browser calls, then closed the browser. The run took 45.18 seconds. Evidence:
`runs/browser-research/model-smoke/result.json`. This validates basic tool/image integration,
not broad web-task success or provider performance.

Continuous streaming passed **seven tab switches in each mode**, including blank and
static pages, without discarding the new tab's only frame. Evidence:
`runs/browser-research/service-smoke-20261014-103113-ca7188/result.json`.

Remote-mode protocol acceptance attached to a separately owned local headed Chrome,
opened a fresh tab, captured a 1280×800 screenshot and disconnected without stopping
the owner browser. Final cleanup stopped both owned daemons. Evidence:
`runs/browser-research/remote-attach-18ebfee6/result.json`. External hosted authentication,
network behavior, file transfer and provider lifecycle have not been tested.

Live frontend QA passed tabs, typing/backspace/wheel, prompts, takeover, desktop and
mobile layouts from 390px to 1280px, all nine chat entries and ten outputs, and a clean console.
The animated cursor's position was checked against its scaled viewport coordinates.
Final Meta+A and Control+A checks each deleted the full input, then saved an exact
multi-character replacement, including a long sentence. The test browser was closed
through the UI and its stopped state confirmed; the disposable fixture server also exited.
Evidence: `runs/browser-research/ui-smoke/verification.json`.

Earlier failures remain recorded, including the prompt-dialog issue and two unconfirmed
input-delivery failures resolved by receive-queue draining. They were not automatically
replayed. Nested-frame completeness remains unverified. A connected socket or animated
cursor alone is not evidence that the viewer displays the state the tools operate on.

The open-web [BrowserArena study](https://arxiv.org/abs/2510.02418) motivates testing
popup handling and navigation recovery. It does not establish a winning provider,
model or observation scheme for this workbench.
