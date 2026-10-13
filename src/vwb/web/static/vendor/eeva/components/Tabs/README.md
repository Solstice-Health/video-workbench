Underlined tabs with an optional count (Eeva V1 board 3). 40px, 24px apart, 14px; the active tab is 600 with a 2px ink underline, the others 500 in secondary text. The count badge is ink on the active tab.

**Use:** Switching views of one thing: Request Detail's Details and Files, the brand library's sources.

**Avoid:** Not for navigation between pages.

**Variants:** count

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `children` | `React.ReactNode` | yes |  |
| `count` | `number` | no | A count badge after the label. Hidden at 0. |

Compose `Tabs` (the root, with `value` or `defaultValue`), a `TabList` of `Tab`s, and one `TabPanel` per value.
