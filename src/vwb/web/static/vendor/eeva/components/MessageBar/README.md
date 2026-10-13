One line of status at the top of the request panel body (Eeva V1 board 5, from C1, C2, E1, B3, G3). Sand band, 13/18 text, a 14px icon aligned to the first line; info and success bars carry an ×. Success is sage: the request is ready to review (A5).

**Use:** `progress` while the agent prepares or updates (Preparing your request…, Updating Audience and Topic…). `info` for what just happened (Updated, Stopped, Set up without reading a file). `success` once, when the request first becomes ready (Ready to review). `error` for a blocking gap (1 required field is still empty).

**Avoid:** One bar at a time. Never in the chat, never a toast. An error about one field also rings that field.

**Variants:** tone: progress | info | success | error; onDismiss (info, success)

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `tone` | `MessageBarTone` | yes |  |
| `children` | `React.ReactNode` | yes |  |
| `onDismiss` | `() => void` | no | Shows the ×. `info` and `success` only: progress and error stay until their state ends. |
| `className` | `string` | no |  |
