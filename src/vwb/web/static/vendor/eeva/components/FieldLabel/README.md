A request field's label (Eeva V1 board 3b, from A1 and A5): 13px secondary text, the red asterisk when required, and a readiness mark before the text.

**Use:** Every row of the request panel. The panel's meta line carries the count until the request is ready.

**Avoid:** Optional fields never get a mark. Not a badge or a dot: the check and the dotted circle are the only readiness signals.

**Variants:** required; readiness: filled | missing | none

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `children` | `React.ReactNode` | yes |  |
| `htmlFor` | `string` | no |  |
| `required` | `boolean` | no |  |
| `readiness` | `FieldReadiness` | no | Required fields only: a check when filled, a dotted ember circle when missing. |
| `className` | `string` | no |  |
