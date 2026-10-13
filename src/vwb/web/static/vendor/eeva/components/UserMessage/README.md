Your message (Eeva V1 board 4, from A1, C1, B1): a bubble on the right, 15/24, at most 78% wide, radius 18, the time beneath and any files above, 6px apart.

**Use:** Everything the person sends, and the answer half of an `AnsweredPair`.

**Avoid:** No avatar, no name. The agent's words are `AgentMessage`.

**Variants:** plain; with files; files only (no bubble); queued (removable)

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `children` | `React.ReactNode` | yes |  |
| `time` | `string` | no | "4:24 PM", beneath the bubble. Replaced by the Queued line while queued. |
| `attachments` | `React.ReactNode` | no | `AttachmentThumb`s (sm), in a row above the bubble. |
| `queued` | `boolean` | no | Sent while the agent works: the bubble at 60% and the Queued line under it. |
| `onRemoveQueued` | `() => void` | no | Queued only: a trash on the Queued line takes the message back. |
| `className` | `string` | no |  |
