The agent at work, then out of the way (Eeva V1 board 4, from A1, A2, B3, G2). Streaming: the Solstice mark turns slowly in the accent beside the thought (15/24 500, no shimmer), the steps collapsed beneath. Done, stopped or failed: one quiet row, the mark in ink and still; click it to open the steps. A step can carry a list (the claims being read); a failed step opens the list so it can't be missed (#1053).

**Use:** Above the agent's reply in a turn. In the feed, leave 12px under a collapsed row (A2 pulls it 10px into the 22px feed gap).

**Avoid:** Not a spinner for other loading. The agent's words after a stop or failure go in an `AgentMessage`, with actions after a failure.

**Variants:** state: streaming | done | stopped | failed; expanded; step lists; a failed step

## Props

| Prop | Type | Required | Notes |
| --- | --- | --- | --- |
| `state` | `ThinkingState` | yes |  |
| `thought` | `string` | no | Streaming: the live thought, replaced as the next one arrives. |
| `summary` | `string` | no | Done: what the turn did, "Read your message". |
| `steps` | `(string \| ThinkingBlockStep)[]` | no | Every step of the turn, in order: a label, or a step with its list and status. |
| `completed` | `number` | no | Steps finished so far. Defaults to all of them once done. |
| `seconds` | `number` | no | Whole seconds the turn has run. |
| `attempts` | `number` | no | Failed: how many tries ran, "3 tries · failed" (G2). |
| `expanded` | `boolean` | no | Controlled open state of the steps list. |
| `defaultExpanded` | `boolean` | no |  |
| `onExpandedChange` | `(expanded: boolean) => void` | no |  |
| `className` | `string` | no |  |
