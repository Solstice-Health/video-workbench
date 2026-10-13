One choice in a multi-select list (Eeva V1 board 3b, from F1): Checkbox, label and subtext. A checked row takes the sunken fill; the whole row is the click target.

**Use:** A stack of rows 2px apart, like the multi-select `ComposerQuestion` (Which platforms?).

**Avoid:** One choice of several is the numbered option rows. A lone checkbox is `Checkbox`.

**Variants:** checked; disabled

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `label` | `string` | yes |  |
| `description` | `string` | no | One line under the label. |
| `checked` | `boolean` | yes |  |
| `onCheckedChange` | `(checked: boolean) => void` | yes |  |
| `className` | `string` | no |  |
