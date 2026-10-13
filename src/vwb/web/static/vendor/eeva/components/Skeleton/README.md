A placeholder bar while a value loads (Eeva V1 boards 5 and 1c). Ink 6% with a 90% white band sweeping every 3.2s; the band is fixed to the viewport, so every bar on screen shares one wave.

**Use:** In place of a field value while the agent prepares or updates it (A1, C1), and inside Notes. The label stays.

**Avoid:** Never in place of a label or a heading. Never gray blocks for a whole layout: only the values that are on their way.

**Variants:** shape: field | notes

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `shape` | `SkeletonShape` | no |  |
| `className` | `string` | no |  |
