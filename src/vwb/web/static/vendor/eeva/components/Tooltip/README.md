A hover and focus tooltip (Eeva V1 board 3). Ink, 12/16 500, radius 6, max 240px, 6px arrow, 8px from its anchor. Shows after 400ms hover or at once on keyboard focus; hides on leave, blur or Escape; flips to stay in view. The board's fade is 120ms; this uses motion.fast (150ms), the nearest token.

**Use:** Label every icon-only button. A rich tooltip explains why, like what a file was attached for.

**Avoid:** Buttons with visible text get none. Never interactive: no links or buttons inside; that is a popover.

**Variants:** label; shortcut; rich (title)

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `content` | `string` | yes | The label, or a rich tooltip's body (up to three lines). |
| `title` | `string` | no | Makes it a rich tooltip: a title above the body, up to 320px wide. |
| `shortcut` | `string` | no | A real keyboard shortcut, shown as a chip after the label. |
| `side` | `'top' \| 'bottom' \| 'left' \| 'right'` | no |  |
| `children` | `React.ReactNode` | yes |  |
| `open` | `boolean` | no | Controlled visibility, for previews and tests. Leave unset for hover and focus. |
