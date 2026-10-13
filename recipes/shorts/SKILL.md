---
name: shorts
description: Make N vertical Shorts from a source video - the Clip and Remake recipes chained and run as a batch - plan, compose, check, repair and deliver. Every screen is remade natively for the vertical frame.
---

# N Shorts from a long video

This chains `clip` (what plays) and `remake` (how each output is rebuilt for the vertical frame). Read both. This file
covers the batch.

## Plan
- Record the job (`job_spec`): N, platform profile (youtube_shorts: 1080x1920, 30 fps, up to 180 s), the length the
  person asked for (if none: 15-60 s for a clip that stands alone), captions (most Shorts are watched without sound),
  the source's brand presence as its terms require, and whether elements may be recreated. Each is an assumed default
  unless the person said it.
- If the request and the source do not fit (for example N long Shorts from a source not much longer than one), say
  so and ask (`human_ask`) how to resolve it, with a recommendation; go ahead with the recommendation.
- Plan the set so the N outputs together serve the brief: distinct, self-contained pieces, or distinct treatments of
  the same material when the length asks for it.

## Compose
- Build shared material once: assets every output will use (a cleaned or extended plate, extracted or recreated
  illustrations, the brand mark), the copy entries, and a short description of the set's visual system (type, colour,
  spacing, motion habits) taken from the source's own look.
- Create every output's canvas (`canvas_new`) and design each one. Keep the set coherent (one visual system) while
  each composition suits its content; generator scripts can share the set's styles.
- Parallel drafting is optional. When the outputs are independent and each is large, you may hand some to composers
  (role composer) with clear briefs: the output id, the time map, what it must communicate beat by beat, the shared
  assets and copy ids, the visual system, and the person's preferences. Review every draft (`canvas_check`,
  `canvas_preview`) and edit the file where you disagree; you are accountable for every committed version. Reject
  drafts that crop the source into boxes where rebuilding the element would read better.

## Check and repair
- Render in batches (`render_output` with outputs), look at every output (`qc_peek`), and run `qc_run`.
- A critic's second look (role critic) is optional, for a large set or high stakes. Act on concrete findings with new
  versions; at most three repairs per output.

## Deliver
- `deliver_submit` every output (batch). Triage will be RED or YELLOW in v0: checks are not yet qualified, so nothing is
  GREEN, and source terms or retyped text can raise BLOCK and HOLD flags. That is expected and honest.
- Reply with one line per output, the decisions that belong to the person, and where to look.
