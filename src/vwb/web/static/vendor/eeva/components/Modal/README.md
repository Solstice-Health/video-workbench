Eeva's dialog (Eeva V1 board 7, from frame D1): 400px, radius 14, a 17px title, a 14/21 message, and the safe action beside a primary or destructive one. Focus starts on the safe action, so Enter never confirms by accident; Escape and the scrim cancel.

**Use:** A question that needs an answer before going on: the major-change confirm (Change and start over).

**Avoid:** Not for picking things; that is `LibraryModal`. Not for news the user does not have to answer.

**Variants:** tone: primary | destructive

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `open` | `boolean` | yes |  |
| `onOpenChange` | `(open: boolean) => void` | yes |  |
| `title` | `string` | yes |  |
| `children` | `React.ReactNode` | yes | The message under the title. |
| `cancelLabel` | `string` | yes | The safe action, first in the footer. It takes focus and closes. |
| `confirmLabel` | `string` | yes |  |
| `tone` | `'primary' \| 'destructive'` | no | `destructive` when confirming loses work. |
| `onConfirm` | `() => void` | yes |  |
