---
name: remake
description: Remake a source video to new specs (for example a 16:9 slide talk into 9:16 Shorts). Every screen is re-designed and re-built natively for the new frame - text retyped, charts and diagrams rebuilt, illustrations recreated, backgrounds extended, motion added - with the source as the reference for content, never as pixels to crop and scale.
---

# Remake

A request to resize, reformat or repurpose a video is a remake. The source decides what is said and shown; the new
spec (frame, length, platform) decides how it looks. Treat each source screen like a brief: what idea it carries,
which facts, labels and references belong to it, which visuals support it. Then build that idea again, natively,
for the new frame. A remake never pastes the old frame into the new one.

## The remake bar
- **Every word is retyped.** Titles, labels, values, footnotes and references are typeset from the copy deck at a
  size a phone can read (`layout_facts` gives the floor). Source text is never shown as image pixels, however small
  or dense it is: retype it (`copy_add` from the OCR'd tracks; three readers settle small text), and lay it out so
  it reads. Dense footnotes and references get their own legible block or screen; they are not shrunk to fit.
- **No source pixels are blown up.** Showing source pixels above their native size (UPSCALED) is a defect. A small
  source icon, logo or illustration is traced to vectors or recreated at full resolution instead.
- **Phone scale.** A Short is watched on a phone, often in a glance. Anchors are large (a chart or illustration
  typically spans the frame's width and a third or more of its height), key numbers are big, headlines are bold,
  and body text sits well above the legibility floor. Lay the screen out over the whole height between the safe
  zone's margins; a screen whose content huddles in one band with empty space below is not finished. When a
  screen holds more than a phone can carry, split it across beats or screens rather than shrinking it.
- **One idea per screen, built in time.** Each screen carries one point; elements arrive when they are spoken
  (list items, bars, numbers, highlights) and the screen holds while a point lands.
- **One visual system.** A set shares type, palette, spacing, page structure and motion habits taken from the
  source's own look (`layout_facts`, frames), applied consistently.
- **Charts become new graphics.** Rebuild data in the form that reads best vertically (flat or isometric bars,
  rings, pictograms, big numbers) with printed values, or values measured with `chart_read` (authored copy with a
  note, flagged for confirmation). Never crop a chart.
- **Every screen has a visual anchor.** One element carries the eye: a full-resolution illustration, a rebuilt
  chart, or a big number. When the narration's subject (an organ, the heart, the lungs, a cell, a patient journey)
  appears in the source only as a small icon or not at all, a recreated hero illustration in the source's visual
  language is usually the strongest anchor: `sunburst_create` with the source icon or frame as a reference. Flat
  icons in rows and grids stay vector (traced); the hero gets the full-resolution treatment.
- **Visuals are crisp and alive.** Illustrations, icons and photos are recreated at full resolution; the anchor
  that carries a spoken beat gets subtle motion (a heart that beats while cardiac risk is discussed, lungs that
  breathe, cells that drift) from `omni_video` on a key colour, looped. Motion explains or emphasises; it never
  decorates a screen that is already busy. Backgrounds are designed or extended to the full frame.

## How to rebuild each kind of element
- **Text** - `copy_add` (source `ocr` with track ids, `shot_title`, or `transcript`). Check `copy_list` for reader
  agreement; where readers still disagree, look at the line yourself (`media_frame` with a crop) and choose.
- **Flat icons, logos, pictograms** - cut out (`sunburst_extract`, method matte keeps the pixels), then
  `asset_trace` to vectors; place as a `shape` with `shape: "svg"` (recolour with `fill`). Sharp at any size.
- **Illustrations and photos** - `sunburst_create` with the source crop as a reference image (`refs`), a prompt
  that names the subject and the source's style, on transparency, at 1024-2048 px; for a set, pass your first
  recreation as a second reference so the style stays consistent. `sunburst_extract` (method sunburst) when the
  element must stay as close to the source as possible.
- **Backgrounds and decorative art** - `sunburst_outpaint` the source's decorative art to 1080x1920, or
  `sunburst_create` a background in the source's palette; `sunburst_inpaint` removes baked-in text first.
- **Charts and data** - `chart_read` (or printed values) into vector shapes. `shape_kit` returns correct geometry
  to paste: isometric 3D bars that grow one by one, progress rings that fill, pictogram grids, arrows; or draw rect
  bars with keyframed boxes yourself. Big numbers are typeset copy.
- **Motion for a visual** - `omni_video` task `frames` with the recreated element as the first frame and
  `key: "auto"`, a prompt for subtle loopable motion (breathing, pulse, slow turn, drift), 4-6 s; place the keyed
  clip as a `clip` layer with `loop: true`. Task `reference` keeps a subject consistent across clips. A first and a
  last frame (for example two views made with `sunburst_create`) give the video model a start and an end to animate
  between; a clip with `loop: false` plays once and then holds its last frame. Keyed clips take `erode` and `feather`
  to pull in and soften the matte edge; `clip_rekey` re-keys a clip with new settings without generating it again.
  A light sweep (`effects.sheen`) gives a still element a moment of life with no video model at all.
- **Transitions between screens** - `canvas_snapshot` (with `no_text: true`) of the last frame of one screen and
  the first frame of the next, then `omni_video` task `frames` (first and last) for a 3-4 s bridge; keep the text
  typeset on top and fade it out and in. A video model distorts text, so never animate text with it.
- **People and real footage** - a `video` layer of the source, only where the recording itself matters (a speaker
  making a point); never for slides. `matte: "person"` (preview with `video_matte`) cuts the speaker out of the
  source background so they stand on your designed page, in sync with the narration.
- **Large canvases** - `canvas_query` finds layers by time, type, id, asset or text without reading the whole file;
  `output_compare` shows what a repair changed between two rendered versions.

## Rules the harness measures
- Typeset text is read back by OCR (TEXT-MISMATCH); text drawn by a model raises GEN-TEXT; source text shown as
  pixels is measured against the legibility floor (LEGIBILITY-BELOW-FLOOR); source pixels above 1.25x are UPSCALED.
- Every generated, traced or regenerated asset carries its provenance flag (GEN-CREATED, GEN-RECOVERED:split,
  fill, extend, animate, trace) with its prompt in the delivery note. That is expected and is how a reviewer sees
  what changed.
- Regulated text of the kept span that appears nowhere is TEXT-DROPPED; text, captions and marks stay inside the
  safe zone (SAFE-AREA).

## Procedure
1. `layout_facts`, `canvas_schema`; look at the source screens you will remake (`index_shots`, `media_frame` with
   show=blocks/text) and write down each screen's idea, facts, references and visuals.
2. Decide the set's visual system, then make the shared assets once (traced marks and icons, recreated
   illustrations, backgrounds) and mint the copy, all in parallel where you can; look at each asset (`asset_view`).
3. `canvas_new`, write the layers screen by screen, with motion timed to the narration.
4. `canvas_check` until it is clean of errors and of the remake defects above; `canvas_preview` at the key beats.
5. Commit, render, `qc_peek`, `qc_run`; repair with new versions.
