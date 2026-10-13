Where a file comes from (Eeva V1 board 3, from M1 and M2): the same dropdown behind the composer's + and the panel's Add files. 232px, radius 10, 36px items with a 15px icon; the hovered item takes an ink 5% fill.

**Use:** Wrap the + or Add files button; the items open the file picker or the brand library.

**Avoid:** Not a general menu: two or three attachment sources only.

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `options` | `AttachMenuOption[]` | yes | Upload files, then Select from brand library (M1). |
| `children` | `React.ReactElement` | yes | The trigger: the composer's + or the panel's Add files. |
| `side` | `'top' \| 'bottom'` | no | Above the composer's + (top, start); below the panel's Add files (bottom, end). |
| `align` | `'start' \| 'end'` | no |  |
| `alignOffset` | `number` | no | Nudges the menu along the trigger, e.g. -6 to line up with the composer's text (M1). |
| `tooltip` | `string` | no | A hover label on the trigger, like Attach files on the composer's +. |
