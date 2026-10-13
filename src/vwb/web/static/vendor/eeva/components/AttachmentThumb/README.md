One attached file (Eeva V1 board 4, from A1, M5, M6): a file-type badge and the name on white, 32px. Above a sent message it carries a soft edge; staged in the composer it takes a firmer edge, an ×, and while uploading a 2px progress bar along the bottom.

**Use:** Above a sent message in `UserMessage`, and staged in the Composer's `attachments` slot with `onRemove`. `onOpen` to preview it.

**Avoid:** Not a link or a tag. A file in the request panel is a reference-file row.

**Variants:** status: ready | uploading | failed; removable (staged); openable

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `name` | `string` | yes |  |
| `fileType` | `AttachmentFileType` | yes |  |
| `status` | `AttachmentStatus` | no |  |
| `progress` | `number` | no | 0 to 100 while uploading: the 2px bar along the bottom. Unset while the server reads the file. |
| `failure` | `string` | no | Failed: why, as the thumb's hover title (no meta line; the boards draw none). |
| `onRemove` | `() => void` | no | Staged in the composer: the × at the end (M5, M6). Its tooltip reads Remove file. |
| `onOpen` | `() => void` | no | The badge and name open the file (a viewer); the × stays separate. |
| `className` | `string` | no |  |
