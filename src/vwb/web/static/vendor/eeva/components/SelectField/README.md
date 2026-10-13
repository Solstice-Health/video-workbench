One choice of a few (Eeva V1 board 3b): the field shell with a trailing chevron. Board 3b draws the closed field only; the open menu here is the smallest one built from existing tokens until it is drawn.

**Use:** Priority, channel, format: a short closed list. With `label`, an auto-width count like Subject lines 3 in Copy's How many row (E5).

**Avoid:** Several choices at once are Checkboxes. Never for navigation.

**Variants:** error; label (compact count select)

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `options` | `SelectOption[]` | yes |  |
| `value` | `string` | no |  |
| `defaultValue` | `string` | no |  |
| `onValueChange` | `(value: string) => void` | no |  |
| `placeholder` | `string` | no |  |
| `label` | `string` | no | Drawn inside the trigger before the value, for a compact count select: "Subject lines 3 ⌄" (E5, Copy's How many row). Pass `className='w-auto'`. Also the default aria-label. |
| `error` | `boolean` | no |  |
| `id` | `string` | no |  |
| `name` | `string` | no |  |
| `className` | `string` | no |  |
