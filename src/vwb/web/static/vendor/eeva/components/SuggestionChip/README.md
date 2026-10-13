A chip above the composer (Eeva V1 board 3). `suggestion` (A7, V1.1 option F): a next message, 30px, 13/500, white at 80% inside a 1px ember ring that deepens on hover; pressed is ink. `generate` (A6): Skip questions, generate my brief. `starter`: the hero Starters (U22).

**Use:** `suggestion`: two to four in the Composer's `suggestions` slot, a fixed row from turn 1, refreshed each turn, Draft copy first on copy-bearing types (N1, N2); clicking sends the text. `generate`: the Composer's `generateChip` slot, right-aligned, while optional questions remain.

**Avoid:** Not while the agent is working. A docked question keeps the row (N1). Never `generate` on a required question.

**Variants:** variant: suggestion | generate | starter; pressed; disabled

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `text` | `string` | yes | The suggestion, sent as the message when clicked. |
| `onSelect` | `(text: string) => void` | yes |  |
| `variant` | `SuggestionChipVariant` | no | `generate`: the pill that skips the optional questions (A6). `starter`: a hero Starter (U22). |
| `icon` | `IconName` | no | The leading glyph. `generate` defaults to the pen; `suggestion` has none (V1.1 option F). |
| `pressed` | `boolean` | no | `starter`: chosen, while it steers the composer (its placeholder). |
| `disabled` | `boolean` | no |  |
| `className` | `string` | no |  |
