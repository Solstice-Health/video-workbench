The agent's words (Eeva V1 board 4, from A2, C2, I1): plain 15/24 in primary text, no bubble, no avatar. Bullets sit 4px under their lead-in at a 20px indent; a table is a small-caps header over hairline rows. Bold, links and bare addresses render inline; the time shows on hover.

**Use:** Every agent reply in the feed, built from blocks (`toAgentBlocks` reads the agent's Markdown into them).

**Avoid:** Never a bubble. A table here is for reading; it is never stored as a table in the panel.

**Variants:** plain; bullets; numbered; table; actions; inline bold and links; hover time

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `blocks` | `AgentMessageBlock[]` | no | Paragraphs, bullet lists and tables, in order. |
| `children` | `React.ReactNode` | no | Shorthand for a single paragraph. |
| `actions` | `React.ReactNode` | no | Buttons 10px under the message, like Try again and Finish with what we have after a failure (G2). |
| `time` | `string` | no | "4:24 PM", top right, shown only on hover or focus (as #1053). |
| `className` | `string` | no |  |
