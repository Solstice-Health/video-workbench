---
name: clip
description: Choose which parts of a source play, for a target length. Whole thoughts, clean openings, claims kept with their qualifiers, cuts in speech gaps. Clip decides time; how the output looks is the remake recipe's job.
---

# Clip

Clip decides time: which parts of the source play, in what order (re-ordered only with care). The narration of the
kept spans is the spine of the output; what is on screen during it is designed separately (see `remake`). The proof
half comes first: every cut is placed and checked by code before anything renders.

## What makes a good clip
- **A whole thought.** Start at a sentence start and end at a sentence end. The listener should not need anything that
  came before. Openings that lean on earlier context ("These…", "However…", "Second,…", "In summary,…") weaken a clip;
  prefer a span that begins with the idea itself, or include the sentence the opening refers to.
- **A hook early.** The first sentence carries the most interesting claim, number or change. If the strongest line is in
  the middle, consider whether a shorter span that starts there still stands alone.
- **Ends that land.** End on a conclusion, not on a lead-in to the next idea ("Pneumonia can also affect daily life.").
- **Distinct outputs.** In a set, each clip covers a different idea; avoid overlapping spans unless the brief asks.
- **Re-ordering is risky in pharma.** Joining non-adjacent sentences can create an implied claim the source never made.
  Prefer contiguous spans; if you join spans, say why in the canvas `intent` and expect a reviewer to check the seam.

## Regulated content (read the dossier's hazards)
- **Claims and their qualifiers stay together** (a statistic with its population, a risk with its condition). A span that
  splits them raises ATOM-BROKEN (HOLD).
- **Benefit and risk.** For product content, a kept benefit claim needs its risk statement in the output
  (RISK-AFTER-BREAK / NO-RISK-IN-VIDEO, HOLD). Disease-awareness content has no product risk statement; say so.
- **Usage terms.** If the source states who it is for or that it must be viewed in its entirety, every excerpt carries
  SOURCE-TERMS (BLOCK) until Regulatory Affairs rules. Do not hide this; tell the person up front and keep going.
- **Job codes.** A derivative needs its own code from MLR (REG-RULE, HOLD). Do not carry the source's code as if it were
  the new piece's.
- **ISI.** If the source carries Important Safety Information, the output needs the full ISI at a legible pace or a
  Regulatory Affairs path (ISI-FIT, ISI-MISSING). Check the dossier for ISI before you plan.

## Procedure
1. `index_transcript` for the whole video; note sentence ids, readers' disagreements and shot changes.
2. `clip_candidates` (with `n` for a set) and your own reading of the transcript and shots. When the cuts are hard to
   choose, a scene_miner (video-native) or a planner_b (another model family) can add an independent view; merge on
   evidence.
3. `clip_refine` every chosen span: it puts each cut in a speech gap at an energy minimum, on a shot cut when one is
   near, with flash-frame guards. Use its frames as the time map.
4. `clip_validate` the whole set: cut-in-word (BOUNDARY-WORD, BLOCK), partial sentences (ATOM-BROKEN), openings that
   lean on context, duration, overlap.
5. After render, `qc_run` with `audio=true` confirms the first and last words are heard in full and captions are in
   sync.

## Checks that decide (verifier)
G0 delivery and length, boundary words, edge words heard, caption sync, cue sheet, plus the standing rules above.
