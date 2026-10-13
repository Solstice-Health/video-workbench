The one button for every surface (Eeva V1 board 3, Oct 5).

**Use:** `primary`: one per view, the main action (Submit request, Send). `secondary`: everything else, and the panel header's waiting state (Preparing or Updating request) with `loading`, 60% and animated dots (board 3). `surface`: the composer's Stop. `destructive`: an action that loses work (Change and start over). `text`: low emphasis (Skip, Finish with what we have, Attach).

**Avoid:** Navigation inside a sentence is `TextLink`. Never two `primary` buttons in one view.

**Variants:** variant: primary | secondary | surface | destructive | text; size: sm | md; iconOnly; disabled; loading; pressed

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `variant` | `ButtonVariant` | no |  |
| `size` | `ButtonSize` | no |  |
| `iconOnly` | `boolean` | no | A 32px circle at any size (board 3: Attach, Send, Stop, Close). Requires `aria-label`; pair it with a `Tooltip`. |
| `loading` | `boolean` | no | Keeps the label, adds the animated dots and blocks clicks until it resolves. |
| `type` | `'button' \| 'submit'` | no | Always a real button; `submit` only inside a form. |
