Eeva is Solstice's interface system: the platform pages, the chat intake and the request panel. It is warm and quiet. Sand neutrals carry almost everything, ink does the talking, and ember appears only where something needs the person's attention. What is here is used; what is used is here.

## Content fundamentals

- Write in sentence case everywhere: buttons, titles, labels, menu items. `label-caps` and `table-head` are the only uppercase, and they are set uppercase by style, not typed in capitals.
- Speak to the person as "you" and keep the agent's voice plain and short. Real copy: "What are we working on?", "Preparing your request…", "Answer the open question to submit", "Skip questions, generate my brief".
- Buttons say exactly what happens: "Submit request", "Change and start over", "Create a new project folder".
- An error says what went wrong and what to do next. One `MessageBar` at a time; never a toast for it.
- No em dashes in product copy. Use a comma, a colon or a full stop. No emoji.

## Visual foundations

### Color

- Page grounds: `bg-canvas` for platform pages and the hero, `bg-chat` for the chat. Cards, panels, fields and modals sit on `surface-default`; `surface-sunken` for recessed boxes, `surface-band` for table header bands, `surface-bubble` for the person's own messages.
- Text in four steps: `text-primary` for body, values and titles; `text-secondary` for labels and captions; `text-tertiary` for placeholders and meta; `text-muted` for timestamps and table headers only. `text-muted` is below 4.5:1 on every ground (3.7:1 on white): keep it to short meta text.
- Ember is the accent, used sparingly. `text-accent` for links and needs-input; `action-accent` for the gentle confirm. `text-inverse` on `action-accent` is 2.7:1, so an ember button carries short labels only.
- `action-primary` (ink) is the main action: one per view. Secondary actions are white with `action-secondary-border`; hover and press tints are ink at 5% to 10%.
- Status always pairs a word or an icon with its tone. `status-success` (sage) means ready; `status-required` (ember) means needs input; `status-danger` (red) means an error; `status-review` means in progress. Each has a `-bg` fill and a 1px `-edge`.
- Edges are ink at an opacity, never a grey hex: `border-subtle` 6% for card and panel edges, `border-divider` 7% for hairlines, `border-default` 12% for fields at rest, `border-strong` 20% on hover, `border-focus` 48% while editing.
- `overlay-scrim` (ink 26%) sits behind every modal, with `overlay-scrim-blur` under the brand library.

### Washes and backings

These are gradients, which the token format cannot hold. Use them exactly:

| Name | Value | Where |
| --- | --- | --- |
| bg-wash | `linear-gradient(180deg, rgba(251,146,60,0.36), rgba(255,255,255,0))` | The top `bg-wash-height` (400px) of platform pages and the hero |
| bg-submit | `radial-gradient(ellipse 85% 60% at 50% -5%, rgba(253,222,186,0.72) 0%, rgba(255,240,222,0.52) 38%, rgba(255,240,222,0) 72%), linear-gradient(180deg, #FBF8F4 0%, #F4EFE9 100%)` | The submitted moment's page |
| header-backing | `linear-gradient(180deg, rgba(243,241,238,0.96) 0%, rgba(243,241,238,0.9) 40%, rgba(243,241,238,0.5) 72%, rgba(243,241,238,0) 100%)` with `header-backing-blur` | The flow header over the chat |
| composer-backing | `linear-gradient(180deg, rgba(243,241,238,0) 0%, rgba(243,241,238,0.72) 22%, rgba(243,241,238,0.96) 46%, #F3F1EE 100%)` with `composer-backing-blur` | Behind the docked composer |

### Type

- Two families. Spectral at 300 (`display`) is for titles only: `hero`, `detail-title`, `doc-title`. Aileron (`ui`) is everything else.
- Chat messages and paragraph copy are `body` (15/24). Field values, row labels and small buttons are `ui` (13/20). A medium button or a tab label is `control` (14/20). Metadata is `caption` (12/16).
- Section labels are `label-caps`; table headers and count badges are `table-head`. Both uppercase with 0.06em tracking.
- Dialogs: `modal-title` (17/24, 600) over `dialog-body` (14/21).
- Aileron ships 100, 300, 400, 600, 700 and 800. `control` and `lead` ask for 500, which renders at 400.

### Space, size and shape

- Space is a 4px base: `space-4` to `space-40`. Every gap, padding and margin takes one of them.
- Controls are `size-sm` (32px: fields, small buttons) or `size-md` (40px: medium buttons, tabs, search).
- Radius is a closed scale: `radius-xs` checkbox, `radius-sm` file badge and tooltip, `radius-md` fields and thumbnails, `radius-xl` tiles and dialogs, `radius-2xl` cards, panels and modals, `radius-bubble` the person's message, `radius-pill` buttons and count badges.

### Elevation and focus

- Surfaces separate by a 1px ink edge plus a soft lift, never a grey border: `elevation-edge` for a white surface on the canvas, `elevation-tile`, `elevation-panel`, `elevation-modal`, `elevation-menu`.
- The composer has no border at all: `elevation-composer` at rest, `elevation-composer-focus` while typing.
- Fields use inset rings: `field-rest`, `field-hover`, `field-editing` (with a 3px halo), `field-error` (1.5px red, no halo).
- `focus-ring` (a 2px white gap and a 2px ink ring) marks every interactive element, on `:focus-visible` only.

### Motion

Every animation names a duration and an easing. Entrances use `standard` or `enter`; exits use `exit` and are shorter than their entrance. Honour reduced motion: fall back to a `fast` crossfade or no motion.

| Duration | Value | Use |
| --- | --- | --- |
| instant | 100ms | Hover, press |
| fast | 150ms | Popovers, crossfades, modal out, the reduced-motion fallback |
| base | 220ms | Rows, rise-ins, collapse, modal in |
| slow | 320ms | Drawers, page enter |
| deliberate | 480ms | Row highlight, saved flash |
| spinner | 900ms, linear, loops | The MessageBar progress spinner |
| pulse | 1400ms, ease-in-out | The three dots on Preparing and Updating |
| ready-glow | 1600ms, once | The panel edge flashing success as it widens |
| skeleton | 3200ms | One wave shared by every skeleton bar |
| spin | 6000ms, linear, loops | The Solstice mark turning while the agent streams |

| Easing | Value | Use |
| --- | --- | --- |
| standard | `cubic-bezier(0.22, 1, 0.36, 1)` | Every UI state change; the default |
| enter | `cubic-bezier(0.16, 1, 0.3, 1)` | Large arrivals |
| exit | `cubic-bezier(0.4, 0, 1, 1)` | Leaving |
| skeleton | `cubic-bezier(0.37, 0, 0.63, 1)` | The skeleton wave |
| ready-glow | `cubic-bezier(0.2, 0.8, 0.2, 1)` | The ready glow |

Rise-ins travel 6px, modals and popovers 8px, drawers 16px. Stagger 40ms for a sequence and 52ms for list items, six at most.

### Loading

- Show no loader for anything faster than 150ms. Once shown, hold it at least 500ms.
- Skeletons stand in for values on their way, never for a whole layout. Labels stay; only the value is a `skeleton-fill` bar.
- Never a grey block page. Static parts render first; live values arrive in place.

## Iconography

- Lucide outline icons at a 2px stroke with round caps and joins, in the text colour of their control. Sizes in use: 12, 13, 14, 15, 16 and 18px.
- Only the icons in `assets/Icons` are registered. A new glyph needs design sign-off first.
- The Solstice mark (`assets/Logos`) is filled, not stroked. It marks the agent's presence: ember and turning while it streams, ink and still when done. It is not a logo lockup.
- No emoji, ever.

## Components

The components load as `window.Eeva` on React 18. Each card's preview is the repository's own Storybook story.

- **Actions:** Button, TextLink, Tabs, Tooltip, SuggestionChip, AttachMenu.
- **Inputs:** TextField, TextArea, RichTextArea, SelectField, UrlField, DateField, SearchField, Checkbox, ChecklistRow, FieldLabel, FieldValue.
- **Feedback:** MessageBar, Skeleton.
- **Chat:** UserMessage, AgentMessage, ThinkingBlock, AttachmentThumb, Composer.
- **Modals:** Modal.
- **Foundations:** Icon, SolsticeMark.

Rules that cut across them:

- One `primary` Button per view. Navigation inside a sentence is a TextLink, never a button.
- One Composer per view, at the foot of the chat. The agent's words are never in a bubble; the person's are, in `surface-bubble`.
- A required field is marked by a red asterisk after its label and nothing else. Optional fields get no mark.
- Status shows in one MessageBar at the top of the panel, never in the chat and never as a toast.

## Not synced

- Gradients are listed under Washes and backings instead of as tokens; motion and loading values are in the tables above, since the token format has no motion family.
- FieldMarker has no card of its own (it shows inside FieldLabel); ModalFrame, Surface, TabList, Tab and TabPanel are on `window.Eeva` as parts.
- Intake-only parts (ComposerQuestion, RequestDocument, ReferenceFilesTable, FlowHeader, SubmitMoment and the rest) and the brand library modal are product features built from Eeva, not part of it.
