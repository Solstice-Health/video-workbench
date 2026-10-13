Multi-line text entry (Eeva V1 board 3b). The same rings as `TextField`, 8px top and bottom inset, sized by its visible lines.

**Use:** Goal (2 lines), Notes (4 lines), a note on a file.

**Avoid:** Not for a single short value; that is `TextField`.

**Variants:** rows: 2 | 4; error

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `rows` | `2 \| 4` | no | 2 for Goal, 4 for Notes (board 3b). |
| `error` | `boolean` | no |  |
