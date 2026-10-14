---
name: Video Workbench · Eeva
description: A quiet editorial interface for an ongoing video-editing conversation.
colors:
  action-primary: "#1D1C1A"
  action-primary-hover: "#3F3C38"
  bg-chat: "#F3F1EE"
  bg-canvas: "#ECE5DF"
  surface-default: "#FFFFFF"
  surface-sunken: "#FAF8F5"
  surface-band: "#F2EEE9"
  surface-bubble: "#E6E2DD"
  text-primary: "#1D1C1A"
  text-secondary: "#5E5A53"
  text-tertiary: "#77716A"
  text-muted: "#8A857F"
  text-accent: "#A85600"
  action-accent: "#EF8006"
  status-success: "#4B7250"
  status-success-bg: "#E9EEE4"
  status-required: "#874400"
  status-required-bg: "#FDEAD3"
  status-danger: "#B4382A"
  status-danger-bg: "#FBEDEA"
typography:
  hero:
    fontFamily: "Spectral, Georgia, serif"
    fontSize: "44px"
    fontWeight: 300
    lineHeight: "48px"
    letterSpacing: "-0.045em"
  detail-title:
    fontFamily: "Spectral, Georgia, serif"
    fontSize: "32px"
    fontWeight: 300
    lineHeight: "38px"
    letterSpacing: "-0.03em"
  body:
    fontFamily: 'Aileron, "Helvetica Neue", Helvetica, sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: "24px"
  ui:
    fontFamily: 'Aileron, "Helvetica Neue", Helvetica, sans-serif'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "20px"
  control:
    fontFamily: 'Aileron, "Helvetica Neue", Helvetica, sans-serif'
    fontSize: "14px"
    fontWeight: 500
    lineHeight: "20px"
  caption:
    fontFamily: 'Aileron, "Helvetica Neue", Helvetica, sans-serif'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: "16px"
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  menu: "10px"
  xl: "14px"
  panel: "16px"
  bubble: "18px"
  pill: "999px"
spacing:
  space-4: "4px"
  space-8: "8px"
  space-12: "12px"
  space-16: "16px"
  space-20: "20px"
  space-24: "24px"
  space-28: "28px"
  space-32: "32px"
  space-40: "40px"
components:
  button-primary:
    backgroundColor: "{colors.action-primary}"
    textColor: "{colors.surface-default}"
    rounded: "{rounded.pill}"
    height: "32px"
  button-primary-hover:
    backgroundColor: "{colors.action-primary-hover}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.pill}"
    height: "32px"
  composer:
    backgroundColor: "{colors.surface-default}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.panel}"
  user-message:
    backgroundColor: "{colors.surface-bubble}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.bubble}"
    padding: "10px 16px"
---

# Design System: Video Workbench

## Overview

The visual authority is the organization's Eeva system. The user requested its
warm, quiet character in a sleek professional workbench, with no decorative
accent bars. Preserve the supplied system rather than creating another identity.
The source tokens and component implementation are vendored locally; see
[docs/EEVA.md](docs/EEVA.md) and the vendor provenance for exact origins.

The interface keeps the conversation central. Sand grounds, readable ink, and
gentle surface separation organize the work. Ember indicates attention and the
agent's presence; it does not decorate every section. Real video previews are
the imagery. There is no marketing illustration or invented output preview.

## Colors

Use semantic `--ds-*` variables, never new approximations of existing colors.
Chat sits on `bg-chat`, navigation on `bg-canvas`, and the composer and workspace
panel on `surface-default`. Recessed activity uses `surface-sunken`; human messages
use `surface-bubble`.

Body copy uses `text-primary`; readable secondary descriptions use `text-secondary`.
Reserve tertiary and muted text for brief metadata, with muted chiefly for timestamps.
Use `text-accent` for links and needs-input copy. Pair every status color with text
or an icon. The context indicator uses ink and sand, and must distinguish an unknown
measurement from a reported zero.

## Typography

Use the established roles in the frontmatter. Spectral 300 is reserved for titles;
Aileron carries messages, controls, metadata, and tool details. Aileron ships 100,
300, 400, 600, 700, and 800; weight 500 falls back to 400. Do not add a monospace
face for tools. Preserve whitespace and wrap long commands in the existing UI face.

Only label-caps and table-head styles uppercase their content. Their uppercase is
applied through CSS, never typed into button copy. Numbers in usage readouts align
using tabular numerals. Dense metadata remains short; explanations use body or UI
type rather than an ever-smaller label.

## Layout

The shell has a 52px activity rail, a 196px file/conversation navigator, a left
chat initially 360px wide, and a flexible canvas. An 8px divider resizes the navigator;
a 12px divider resizes chat against the canvas. Both derive bounds from available
space, support pointer and keyboard input, and retain preferred widths across reloads.
The navigator can be hidden from its rail control; chat can be collapsed. Canvas tabs have explicit close controls, keyboard
arrow navigation, and optional second groups. Groups can be joined without losing
tabs; opening an existing file activates it rather than duplicating it.

Below 768px, Chat and Canvas are separate visible views and navigation becomes a
drawer. Below 640px, the rail is 44px and header controls wrap. Hidden drawers are
removed from keyboard navigation, Escape dismisses the drawer, and focus returns
to the invoking control. Browser streams stop when their canvas view is hidden.
Use 4px spacing steps and the source breakpoint scale.

The current shell was checked live at 1280×720, 1024×600 and 390×600. The checks
covered actual file listing, paginated directories, isolated HTML dependencies,
image/text split views, tab closure, import-event replay prevention, composer
visibility and real integration availability. PDF and browser checks are recorded
in docs/UI.md. Older browser checks also covered 640×560.

## Elevation & Depth

Surfaces separate with a fine ink edge and a soft lift. Use `elevation-edge` for
small activity surfaces, `elevation-panel` for the workspace, `elevation-menu` for
the context popover, and the composer-specific shadows for the input area.
Do not stack two composer shadows when extending its footer.

The composer gains depth on focus. Keyboard focus uses the source white gap and
ink ring only on `:focus-visible`. Hairlines are the `border-divider` token; fields
use inset rings. Preserve the source header and composer backings. The sidecar
records exact shadow and motion values that do not fit frontmatter.

## Shapes

Use the closed radius scale: pill actions, rounded fields and tool cards, a soft
panel and composer, and a slightly rounder human-message bubble. Use ink-opacity
edges instead of grey border hex values. A tool card has no colored left border.
The context pie is a 16px circle, paired with a readable label or percentage.

## Components

Use the real Eeva Button, Tooltip, UserMessage, AgentMessage, Composer, MessageBar,
Tabs, Icon, and SolsticeMark components. Check their source
contracts before adapting them; the documentation in `docs/EEVA.md` calls out
empty-step disclosures and option-only Composer submissions.

Workbench extensions follow these rules:

- **Activity disclosure:** a quiet labeled toggle exposes all available public
  reasoning summaries and activity. Tool calls remain compact, with expandable
  input and output. Preserve stopped and failed states rather than calling every
  completed operation successful.
- **Question queue:** a single docked question area includes previous and next
  controls, current position, option labels, optional descriptions, and an explicit
  selected state. The selection remains a draft until sent. The implementation uses
  named option buttons with `aria-pressed` in a labeled group. If changing to radios,
  supply their full keyboard behavior. Do not make color the only cue.
- **Context popover:** the footer indicator opens the latest reported context,
  known token breakdown, conversation totals, and compaction action. Label cached
  input and reasoning as subsets, and keep cumulative billing separate from
  context occupancy. Missing counts say they are not reported.
- **Sending controls:** Send queues during work. Send now explicitly stops the
  current turn and continues with the new message. Queued messages are visible and
  removable. A stopped queue needs a visible resume path. No action implies the
  ability to alter a token stream already being emitted.
- **Browser:** A canvas tab contains native browser tabs and compact address
  controls. Take control and Return to agent explicitly govern interaction. The
  real SolsticeMark follows verified agent coordinates and is hidden during human
  control. Letterboxing uses the same transform for cursor and pointer input.
  Page dialogs remain in the panel with plain text and secondary actions.
- **Workspace:** A lazy file explorer opens real request assets in canvas tabs.
  Images and videos preserve their proportions; HTML runs in an opaque sandbox;
  text stays literal. PDF pages use bounded host rendering, with previous/next
  controls. Outputs retains its real versions, flags and review links.
- **Integrations:** The composer dropup selects plugins or MCP tools as message
  context. Selection is separate from authentication. Actual status is paired with
  words and an icon; an available but disconnected service is not shown as ready.
  Public connection settings use a focused modal; secrets never enter the UI.

Use one MessageBar at the top of the current panel, never a status bubble in
message history. Its error, progress, and recovery copy must match actual server
state. Default motion is 100ms for state feedback, 150ms for popovers, and 220ms
for disclosure. The working mark rotates over 6000ms; reduced motion stops it.

## Do's and Don'ts

- Do keep one primary action and one composer in the view.
- Do keep agent replies as plain text and human replies in sand bubbles.
- Do use sentence case, specific actions, accessible names, and source tokens.
- Do preserve unknown usage values and real asynchronous state.
- Don't add decorative accent rails, gradients beyond source backings, or emoji.
- Don't add a dark theme or new icon vocabulary without an explicit design change.
- Don't imply that model reasoning summaries expose hidden reasoning.
- Don't introduce placeholder projects, video results, or token counts into real data.
