The value slot of a request panel row in one of its states (Eeva V1 board 3b): the agent loading it, the agent having updated it, required and missing, or left empty by a Stop. Renders the right field for `kind`.

**Use:** Every row of RequestDocument. `loading` while the agent writes the value (A1, C1); `updated` after it rewrote one (C2), cleared by the first edit; `missing` once a required question is skipped (E1); `stopped` for a value the agent never reached (B3). `checklist` for several choices (Banner sizes, Platform), with `custom` for a typed one.

**Avoid:** Not for Notes (`RichTextArea`) or a file's note. No hint text under the field: the panel's MessageBar says what is wrong.

**Variants:** kind: text | textarea | select | checklist | url; state: idle | loading | updated | missing | stopped

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `kind` | `FieldValueKind` | no |  |
| `state` | `FieldValueState` | no |  |
| `value` | `string` | yes | `checklist` values are comma-joined. |
| `onValueChange` | `(value: string) => void` | no |  |
| `options` | `SelectOption[]` | no | `select` and `checklist`. |
| `custom` | `FieldValueCustom` | no |  |
| `placeholder` | `string` | no |  |
| `id` | `string` | no |  |
| `className` | `string` | no |  |
