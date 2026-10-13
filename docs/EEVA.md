# Eeva in Video Workbench

Eeva is the organization's interface system and the visual authority for this UI.
The [organization artifact](https://claude.ai/artifact/Duj6yyXvM9NpMGedFMsKKg)
identifies `Solstice-Health/Solstice-Frontend@25c18f2d7`, synced 2026-10-07.
The implementation uses its editorial light theme, Aileron, Spectral 300, ink,
sand neutrals, and restrained ember. There is no invented dark theme.

## Local sources and loading

The vendored system is at `src/vwb/web/static/vendor/eeva/`:

- `README.md`, `tokens.json`, and `components/<Name>/README.md` hold usage rules.
- `components/index.d.ts` describes props. `source/` contains the original TSX.
- `components/bundle.js` exposes 33 React 18 exports on `window.Eeva`.
- `PROVENANCE.md` records extraction, dependencies, licenses, and verification.

The browser bundle was reconstructed from the exact source revision because the
artifact export did not contain its original compiled JS. React and ReactDOM
18.3.1, htm 3.1.1, all Aileron faces, and Spectral 300 are served locally.

Load `components/bundle.css`, **then `tokens.css`**, then application CSS. The first
sheet contains source-token aliases; the second resolves them to the artifact's
values and supplies typography and font declarations. Load React, ReactDOM, Eeva,
and finally application JS. Use one `Surface background="chat"` around the page.

## Rules that survive customization

- Use Aileron for controls and messages; Spectral 300 is for titles. Body is 15/24,
  UI is 13/20, control is 14/20, and caption is 12/16. Aileron has no 500 face.
- Agent replies are plain text without an avatar or bubble. Human messages use
  `UserMessage`, its right-aligned sand bubble, and its queued state when applicable.
- Use one Composer and one primary action. Send is the primary action. Stop uses
  the surface button; secondary actions stay quiet.
- Put status in one MessageBar outside the message history. Errors describe the
  problem and the next action. Do not use toasts or decorative accent rails.
- Use the ink edge and elevation tokens, the closed radius scale, and 4px spacing
  steps. Ember is reserved for attention, links, the working mark, and chip rings.
- Every interactive control needs a visible keyboard focus ring. Icon-only buttons
  require an accessible name and Tooltip. Use the registered Lucide icons only.
- Keep sentence case, plain copy, and explicit button labels. No emoji or em dashes
  in product copy. Respect reduced motion.

## Component contracts to check before changing the UI

`Composer` owns a textarea and passes trimmed text to `onSend`. `working` displays
Stop; **the caller implements queuing**. `canSend` controls eligibility, but the
original component also requires text or a truthy `attachments` slot before calling
`onSend`. An option-only answer needs an explicit integration, not just `canSend=true`.
Workbench supplies a visible selected-answer chip in that slot when no files are staged.
Pass an absent `question` as `undefined`, because any truthy React node creates the
question divider even if that node later renders nothing.

`ThinkingBlock` supports streaming, done, stopped, and failed. Its expand control
exists only when `steps.length > 0`. `expanded` and `onExpandedChange` do not make
an empty step list interactive. A custom transcript disclosure must own its own
accessible toggle or supply meaningful steps. Provider-emitted reasoning summaries
are the available stream; do not imply access to hidden reasoning.
Workbench uses its own labeled disclosure with Eeva's SolsticeMark and tokens so
the full available transcript can be expanded without a fabricated step list.

`AgentMessage` accepts text, bullets, numbered lists, and tables. Its inline parser
supports bold and HTTP(S) links; it is not a full Markdown renderer. Code fences
need a separate rendering decision. `UserMessage queued` provides the dimmed
bubble and removal control; removal is implemented by the caller.

`Tooltip` is noninteractive. `Tabs` delegates keyboard behavior to Radix. Eeva
exports no generic popover, tool-call card, context chart, or question navigator;
these are Workbench components composed from existing tokens. See `../DESIGN.md`
for their rules.

## Verification scope

The local bundle exports, source-token coverage, and current UI component contracts
were checked. The application was exercised with DOM-based interaction smoke checks.
Desktop and mobile viewport layout remain visually unverified: the controlled
browser returned `ERR_BLOCKED_BY_CLIENT` for the local workbench URL. This is not
a claim of completed screenshot or browser-layout review.
