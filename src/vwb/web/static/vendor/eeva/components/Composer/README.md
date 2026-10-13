Where the person writes (Eeva V1 board 4, from A5 and B1). White, radius 16, the composer lift and no grey border; it lifts further on focus. At rest: placeholder, Attach and Send. While the agent works Send stays and queues, and a round Stop appears beside it.

**Use:** Once, at the foot of the chat. Questions, chips and files go in its slots.

**Avoid:** Not a form field: one-line answers in the panel are `TextField`. Never two composers in a view.

**Variants:** at rest; working; with files; with a question; with chips; docked; attach menu; dictation; dragging

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `value` | `string` | yes |  |
| `onValueChange` | `(value: string) => void` | yes |  |
| `onSend` | `(value: string) => void` | yes | Enter or Send, with the trimmed text. While the agent works the caller queues it. |
| `canSend` | `boolean` | no | False while nothing can go yet (files still reading or failed): Send disables and Enter does nothing. |
| `onAttach` | `() => void` | no | Shows the Attach button. |
| `attachOptions` | `AttachMenuOption[]` | no | Attach opens the AttachMenu with these sources (Upload files, Select from brand library) instead of calling `onAttach`. |
| `dictation` | `ComposerDictation` | no | Shows the mic, left of Send. |
| `onPaste` | `React.ClipboardEventHandler<HTMLTextAreaElement>` | no | The text box's paste, so the caller can turn a long one into an attachment. |
| `onStop` | `() => void` | no | With `working`, shows Stop 6px left of Send. |
| `working` | `boolean` | no | The agent is working: Send stays and queues, Stop appears. |
| `placeholder` | `string` | no |  |
| `label` | `string` | no | The text box's accessible name. |
| `attachments` | `React.ReactNode` | no | Staged `AttachmentThumb`s (with `onRemove`), wrapping in a row above the text (M5, M6). |
| `dragging` | `boolean` | no | A file is being dragged over the page: the box shows Drop to attach (M7). The caller handles the drop. |
| `question` | `React.ReactNode` | no | A docked `ComposerQuestion`, above a hairline; the text box below becomes its typed answer. |
| `suggestions` | `React.ReactNode` | no | Two to four `SuggestionChip`s, 8px above the composer. |
| `generateChip` | `React.ReactNode` | no | A `SuggestionChip variant='generate'`, right-aligned 8px above the composer. |
| `docked` | `boolean` | no | Pinned to the foot of the feed: adds the backing that blurs and fades the feed behind it. |
| `className` | `string` | no |  |
