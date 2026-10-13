A date (Eeva V1.1, boards 6 to 9: Tracking details' optional dates). The field shell with a calendar icon and mm/dd/yyyy; reads as `Oct 16, 2026` at rest and becomes the browser's native date entry and picker while editing.

**Use:** PRC submission, AfD target and Live date, under Dates · optional in Tracking details.

**Avoid:** Not for a date range or a time.

**Variants:** error

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `value` | `string` | no | ISO date, `YYYY-MM-DD`. Empty string for no date. |
| `defaultValue` | `string` | no |  |
| `onValueChange` | `(value: string) => void` | no |  |
| `error` | `boolean` | no |  |
| `placeholder` | `string` | no |  |
| `className` | `string` | no |  |
