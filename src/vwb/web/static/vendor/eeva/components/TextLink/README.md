A link (Eeva V1 board 3). Links navigate; buttons do things. Board 3 draws rest only; hover is the smallest change that reads as a link.

**Use:** `inline`: an action inside a sentence, like View full chat history. `url`: a web address shown as itself. `back`: View all requests, above a page title.

**Avoid:** Never for an action that does not navigate. A standalone Exit is a `text` Button.

**Variants:** kind: inline | url | back; external

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `href` | `string` | yes |  |
| `kind` | `TextLinkKind` | no |  |
| `external` | `boolean` | no | Opens in a new tab. |
| `className` | `string` | no |  |
| `children` | `React.ReactNode` | yes |  |
