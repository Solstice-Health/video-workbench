# Canvas file reference (vwb.canvas/2)

A canvas file describes one output. You write it; the harness renders, measures and checks it. Nothing is
placed for you and there is no house style: which elements exist, how each is made, where it sits, how it
moves and when it appears is your design. The examples below show syntax only; their values are
placeholders, not a look to copy.

Work on `outputs/<id>/canvas.json` in the work folder, check it with `canvas_check`, look with
`canvas_preview`, commit with `canvas_commit` (job lead only).

## Units and time
- Output pixels: x right, y down, origin top-left. YouTube Shorts is 1080 x 1920 at 30 fps.
- Source pixels (for a `video` layer's `crop`): the source frame, for example 1920 x 1080.
- Frames: output frames from 0. Spans are half-open `[start, end)`.
- `time_map` lists the source frames the output plays, in order: `[{"media": "src1@<rev>", "src_in": 1410, "src_out": 1977}]`.
  Several spans are joined in order. The audio (the narration) follows the time map, and it is the clock of
  every layer, whatever the layer shows.

## Animated values
`box`, `crop`, `opacity`, `rotate`, a shape's `fill` and `draw`, a stroke's `color` and a text `color` are
either static or a list of keyframes:
```json
[{"f": 0, "v": [0, 652, 1080, 720]}, {"f": 90, "v": [0, 400, 1080, 1080], "ease": "inout"}]
```
`ease` shapes the move from the previous key to this one: `hold` (jump at f), `linear`, `in`, `out`, `inout`.
Before the first key the first value holds; after the last key the last value holds. Colours
(`"#RRGGBB"` or `"#RRGGBBAA"`) interpolate too.

## Layers
`layers` is drawn bottom to top. Every layer has `id` (unique), `type`, optional `span` (`[o0, o1)`, default
the whole output), `opacity` (0 to 1), `rotate` (degrees clockwise about the box centre), `role` and
`note` (why it exists; reviewers read it). `role` is one of `content` (something a viewer must read or
see), `background`, `brand`, `decor`, `text`.

### image
A still from the asset registry, fitted into its box (alpha is kept). Assets come from `sunburst_create`,
`sunburst_extract`, `sunburst_inpaint`, `sunburst_outpaint`, `canvas_snapshot`, `asset_recover` or an upload.
Source pixels shown above 1.25x their native size are UPSCALED, and source text inside an image is measured
against the legibility floor: retype text and trace or recreate small art instead.
```json
{"id": "<id>", "type": "image", "role": "content", "asset": "slot:<name>@<rev>", "box": [x, y, w, h],
 "fit": "contain", "align": "center", "valign": "middle"}
```
- `fit`: `contain` shows the whole image inside the box (aligned by `align`, `valign`); `cover` fills the
  box and crops the overflow.
- `mask` and `effects` as for video (below).

### clip
A moving image from the registry (an `omni_video` clip; keyed clips carry alpha) on its own clock.
```json
{"id": "<id>", "type": "clip", "asset": "slot:<name>@<rev>", "box": [x, y, w, h], "start": 0,
 "clip_in": 0.0, "rate": 1.0, "loop": false, "fit": "contain"}
```
- Clip time at output frame f is `clip_in + rate * (f - start) / fps` seconds. After the end the clip loops
  (`loop`) or holds its last frame; before `start` it holds its first frame. Use `span` to show it only
  part of the time.
- `fit`, `align`, `valign`, `mask`, `effects` as for image.

### shape
Vector graphics rendered as SVG or CSS: rectangles, circles and ellipses (circle in a non-square box),
lines, and paths. Build charts, diagrams, timelines, maps, icons, rules and highlights from these.
```json
{"id": "<id>", "type": "shape", "shape": "rect", "box": [x, y, w, h], "fill": "#RRGGBB", "radius": 0}
{"id": "<id>", "type": "shape", "shape": "path", "box": [x, y, w, h], "viewbox": [100, 100],
 "d": "M0 100 L100 0", "stroke": {"width": 2, "color": "#RRGGBB", "cap": "round"},
 "draw": [{"f": 0, "v": 0}, {"f": 30, "v": 1}]}
```
- `fill` (animatable), `stroke` `{width, color (animatable), dash, cap}`, `radius` (rect corners),
  `gradient` (colour stops) with `gradient_angle` (CSS degrees).
- `path`: SVG path data `d` in a coordinate space of `viewbox` `[w, h]` (default: the box size), scaled onto
  the box. `draw` (0 to 1, animatable) reveals the stroke along the path.
- `paths`: several paths in the same `viewbox`, each `{d, fill, stroke, opacity, translate}` - for example the
  three faces of an isometric bar, or a drawn icon.
- `shape: "svg"` with `asset`: a traced vector asset (`asset_trace`), fitted into the box; `fill` recolours every
  path. It stays sharp at any size.
- A bar that grows is a rect (or a `paths` group) whose `box` is keyframed; a ring that fills is a circular path
  whose `draw` is keyframed. `progress: true` makes a rect's width follow the timeline.

### text
Typeset copy. Text comes only from the copy deck: mint a string with `copy_add`, then cite its id.
```json
{"id": "<id>", "type": "text", "copy_id": "<copy id>", "box": [x, y, w, h],
 "style": {"font": "<family>", "weight": 600, "size": 48, "color": "#RRGGBB"},
 "animate": {"enter": "none", "exit": "none", "frames": 12}}
```
- `style` must name `font` (a family of the font library; `layout_facts` lists families and weights),
  `weight`, `size` and `color`. Optional: `min_size` (default half of size), `italic`, `align`, `valign`,
  `line_height`, `letter_spacing`, `uppercase`, `shadow`, `stroke`, `background` (`#RRGGBBAA`), `padding`,
  `radius`, `accent_bar` (colour).
- The type shrinks to fit the box and never truncates (down to `min_size`); make boxes big enough.
- `animate.enter`: `none`, `fade`, `rise`, `pop`, `wipe`, `type`; `exit`: `none`, `fade`, `sink`; `frames`.
- Superscripts: `^{1,2}` in copy renders as a superscript (reference markers on slides mint this way).

### captions
Word-timed captions from the two-reader transcript; pages are derived when you commit.
```json
{"id": "<id>", "type": "captions", "box": [x, y, w, h], "mode": "plain", "max_chars": 24, "max_lines": 2,
 "style": {"font": "<family>", "weight": 700, "size": 52, "color": "#RRGGBB"}}
```
- `mode`: `plain`, or `karaoke` with `style.highlight` (the word being spoken) and `style.muted` (words to come).

### video
The time-mapped source frame through a declared crop: for a speaker, real footage, or anything where the
recording itself matters.
```json
{"id": "<id>", "type": "video", "role": "content", "source": "src1@<rev>", "box": [x, y, w, h],
 "crop": [x, y, w, h], "fit": "window", "mask": {"shape": "rounded", "radius": 24}}
```
- `crop`: the source window `[x, y, w, h]` (animatable), or `"full"`. `fit`: `window` maps the crop exactly
  onto the box, so their aspects must match (no squash; `reframe_solve` gives matching keyframes for any
  box); `cover` fills the box from the crop's centre.
- `mask.shape`: `rect`, `rounded` (with `radius`), `circle`, `ellipse`.
- `effects` (video, image, clip): `blur` (sigma, output px), `dim` (0 to 1), `tint` (`#RRGGBB`) with
  `tint_amount`, `shadow` `{blur, opacity, dy}`, `border` `{width, color}`, and `sheen` - a soft band of light
  that sweeps across the layer inside its own shape (a glare, a shine):
  `{start, frames, angle, width, strength, color, repeat}` (output frames; `angle` tilts the band from vertical;
  `width` is a fraction of the layer's width; `repeat` is frames from one sweep to the next, 0 = once).

## What the harness measures (design for it)
- Every typeset string is read back from the rendered pixels by two OCR engines (TEXT-MISMATCH if wrong).
- Regulated text of the kept span (claims, statistics, footnotes, references, job codes, usage terms) must
  appear in a source window or as typeset copy; otherwise TEXT-DROPPED.
- Every image and clip carries its asset's provenance: generated or regenerated pixels raise GEN-CREATED or
  GEN-RECOVERED (split, fill, extend, animate), text drawn by a model raises GEN-TEXT, a generated face
  SYNTH-PERSON, a weak matte MATTE-RISK. The delivery note lists each asset with its prompt.
- For every `content` video window: source text it cuts at an edge (TEXT-CLIPPED), on-screen type size
  against the legibility floor, faces cut (FACE-CUT), upscale above 1.25x (UPSCALED), camera motion.
- Text, captions and brand layers belong inside the platform safe zone (SAFE-AREA). `layout_facts` gives it.
- Cuts must land in speech gaps (BOUNDARY-WORD); the time map comes from `clip_refine`.

## Advisors (optional, they never decide for you)
- `focus_find(spans)`: what each spoken beat refers to on screen (a box in source pixels, matched text).
- `reframe_solve(spans, box, focus)`: crop keyframes for a source window at a box you chose.
- `clip_candidates`, `clip_refine`, `clip_validate`: where to cut.
