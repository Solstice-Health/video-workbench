The request's Notes (Eeva V1 board 3b, from I4). Edited formatted, stored as Markdown; a toolbar with bold, italic, bulleted and numbered lists, link and table appears on focus. Grows with its content.

**Use:** The Notes section of the request panel: anything the team should know that has no field.

**Avoid:** Not for a short value or a file's note (`TextField`). No headings, images or colours: only what the toolbar writes.

**Variants:** loading; empty; filled; focused (toolbar)

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `value` | `string` | yes | Markdown. |
| `onChange` | `(markdown: string) => void` | no | Markdown, on every edit. |
| `onBlur` | `() => void` | no | When focus leaves the field and its toolbar: a natural moment to save. |
| `placeholder` | `string` | no |  |
| `loading` | `boolean` | no | A Skeleton inside the field while the agent writes the notes (A1). |
| `autoFocus` | `boolean` | no |  |
| `id` | `string` | no |  |
| `className` | `string` | no |  |
