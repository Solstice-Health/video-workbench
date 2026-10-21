"""Canvas schema vwb.canvas/2: an open scene graph that agents author like code.

Nothing about layout lives in code. The agent decides which elements exist, where they
sit, how they move and when they appear; the harness only renders, measures and checks.

The fixed core (spec D37) stays small and explainable:
  time_map  which source frames the output plays, in order (half-open frame spans); the
            audio follows it, and it is the clock of every layer
  layers    bottom to top; each is video | image | clip | shape | text | captions
  copy      every string that is typeset; text layers cite copy ids, never raw text

There is no house style: a text layer names its font, weight, size and colour, and the
schema supplies no look of its own (no default colours, effects or motion).

Any numeric property can be static or animated. Animated values are keyframe lists:
  [{"f": output_frame, "v": value, "ease": "hold|linear|in|out|inout"}]
where `ease` shapes the move from the previous key to this one ("hold" jumps at f).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator

from vwb.config import GENERATORS_DIR

SCHEMA = "vwb.canvas/2"
Ease = Literal["hold", "linear", "in", "out", "inout"]
FONT_MANIFEST = GENERATORS_DIR / "remotion" / "public" / "fonts" / "manifest.json"


@lru_cache(maxsize=1)
def font_library() -> dict[str, dict[str, Any]]:
    """The families the renderer has pinned (open licences), with their weights and italics."""
    lib = {}
    for e in json.loads(FONT_MANIFEST.read_text()):
        lib[e["family"]] = {"category": e.get("category"), "license": e.get("license"),
                            "weights": sorted({f["weight"] for f in e["faces"] if f["style"] == "normal"}),
                            "italic": sorted({f["weight"] for f in e["faces"] if f["style"] == "italic"})}
    return lib


class Key(BaseModel):
    f: int
    v: Any
    ease: Ease = "inout"


Box = Union[list[float], list[Key]]     # [x, y, w, h] in output pixels, or keyframes of it
Scalar = Union[float, list[Key]]
Color = Union[str, list[Key]]           # "#RRGGBB" or "#RRGGBBAA", or keyframes of it


class Span(BaseModel):
    """Source frames [src_in, src_out) of one media revision, played in order."""
    media: str
    src_in: int
    src_out: int

    @field_validator("src_out")
    @classmethod
    def _positive(cls, v: int, info) -> int:
        if v <= info.data.get("src_in", 0):
            raise ValueError("span must have src_out > src_in")
        return v


class Mask(BaseModel):
    shape: Literal["rect", "rounded", "circle", "ellipse"] = "rect"
    radius: float = 0.0                 # rounded corners, output px


class Shadow(BaseModel):
    blur: float = 28.0
    opacity: float = 0.45
    dy: float = 10.0


class Border(BaseModel):
    width: float = 0.0
    color: str = "#FFFFFF"


class Sheen(BaseModel):
    """A soft band of light that sweeps across a layer, inside its own shape: a glare, a shine."""
    start: int = 0                                  # output frame where the first sweep begins
    frames: int = Field(30, gt=0)                   # length of one sweep
    angle: float = 20.0                             # band tilt from vertical, degrees; the band travels left to right
    width: float = Field(0.25, gt=0)                # band width, as a fraction of the layer's width
    strength: float = Field(0.6, ge=0, le=1)        # how far the band lightens toward `color`
    color: str = "#FFFFFF"
    repeat: int = Field(0, ge=0)                    # frames from one sweep's start to the next; 0 = once


class Effects(BaseModel):
    blur: float = 0.0                   # gaussian sigma in output px
    dim: float = 0.0                    # 0..1 darkening
    tint: str | None = None             # "#RRGGBB" mixed in by tint_amount
    tint_amount: float = 0.0
    shadow: Shadow | None = None
    border: Border | None = None
    sheen: Sheen | None = None          # image, clip and video layers


class LayerBase(BaseModel):
    id: str
    role: Literal["content", "background", "brand", "decor", "text"] = "content"
    span: list[int] | None = None       # [o0, o1) output frames; None = the whole timeline
    opacity: Scalar = 1.0
    rotate: Scalar = 0.0                # degrees clockwise about the box centre
    note: str = ""                      # why this layer exists (for reviewers and the critic)


class VideoLayer(LayerBase):
    type: Literal["video"] = "video"
    source: str                         # media ref, e.g. "src1@83cfdc4b6693" (follows the time map)
    box: Box
    crop: Union[Literal["full"], list[float], list[Key]] = "full"   # source window, source px
    fit: Literal["window", "cover"] = "window"   # window: crop aspect must equal box aspect; cover: fill box
    matte: Literal["none", "person"] = "none"    # person: only the people show (video matting), in sync
    mask: Mask = Field(default_factory=Mask)
    effects: Effects = Field(default_factory=Effects)


class ImageLayer(LayerBase):
    type: Literal["image"] = "image"
    asset: str                          # slot:<name>@<rev> from the registry (PNG, with alpha when it has one)
    box: Box
    fit: Literal["contain", "cover"] = "contain"
    align: Literal["left", "center", "right"] = "center"
    valign: Literal["top", "middle", "bottom"] = "middle"
    mask: Mask = Field(default_factory=Mask)
    effects: Effects = Field(default_factory=Effects)


class ClipLayer(LayerBase):
    """A moving image from the registry (for example a generated or keyed clip) on its own clock.

    Clip time t (seconds) at output frame f is clip_in + rate * (f - start) / fps. Past the end
    the clip loops or holds its last frame."""
    type: Literal["clip"] = "clip"
    asset: str                          # slot:<name>@<rev> of a video asset (keyed clips carry alpha)
    box: Box
    start: int = 0                      # output frame where clip time clip_in plays
    clip_in: float = 0.0                # seconds into the clip at `start`
    rate: float = 1.0
    loop: bool = False
    fit: Literal["contain", "cover"] = "contain"
    align: Literal["left", "center", "right"] = "center"
    valign: Literal["top", "middle", "bottom"] = "middle"
    mask: Mask = Field(default_factory=Mask)
    effects: Effects = Field(default_factory=Effects)


class Stroke(BaseModel):
    width: float
    color: Color
    dash: list[float] | None = None     # SVG dash pattern, output px
    cap: Literal["butt", "round", "square"] = "butt"


class PathSpec(BaseModel):
    """One path of a multi-path shape (for example one face of a 3D bar)."""
    d: str
    fill: str | None = None
    stroke: Stroke | None = None
    opacity: float = 1.0
    translate: list[float] | None = None


class ShapeLayer(LayerBase):
    """Vector shapes. `path` takes SVG path data in a coordinate space of `viewbox` [w, h]
    (default: the box size) scaled onto the box; `paths` draws several paths in that space;
    `svg` draws a traced vector asset (`asset`), recoloured by `fill` when given. `draw`
    reveals strokes from 0 to 1."""
    type: Literal["shape"] = "shape"
    shape: Literal["rect", "circle", "line", "path", "svg"]
    paths: list[PathSpec] | None = None
    asset: str | None = None                # shape=svg: slot ref of a vector asset (asset_trace)
    box: Box
    fill: Color | None = None
    stroke: Stroke | None = None
    radius: float = 0.0                 # rect corner radius, output px
    gradient: list[str] | None = None   # colour stops along gradient_angle
    gradient_angle: float = 180.0       # CSS degrees: 180 runs top to bottom
    d: str | None = None                # path data (shape=path)
    viewbox: list[float] | None = None  # [w, h] of the path coordinates
    draw: Scalar = 1.0                  # fraction of the path stroke drawn
    progress: bool = False              # width follows the timeline (0 at the start, full at the end)


class TextStyle(BaseModel):
    font: str                           # a family from the font library (layout_facts lists it)
    weight: int
    size: float                         # largest size; the renderer shrinks to fit, never truncates
    color: Color
    min_size: float | None = None       # smallest size it may shrink to (default: half of size)
    italic: bool = False
    align: Literal["left", "center", "right"] = "left"
    valign: Literal["top", "middle", "bottom"] = "top"
    line_height: float = 1.2
    letter_spacing: float = 0.0
    uppercase: bool = False
    shadow: bool = False
    stroke: Border | None = None
    background: str | None = None       # box fill behind the text, "#RRGGBBAA"
    padding: float = 0.0
    radius: float = 0.0
    accent_bar: str | None = None       # a short bar above the text, colour


class Animate(BaseModel):
    enter: Literal["none", "fade", "rise", "pop", "wipe", "type"] = "none"
    exit: Literal["none", "fade", "sink"] = "none"
    frames: int = 12


class TextLayer(LayerBase):
    type: Literal["text"] = "text"
    role: Literal["content", "background", "brand", "decor", "text"] = "text"
    copy_id: str
    box: Box
    style: TextStyle
    animate: Animate = Field(default_factory=Animate)


class CaptionStyle(TextStyle):
    highlight: str | None = None        # colour of the word being spoken (karaoke)
    muted: str | None = None            # colour of words not yet spoken (karaoke)


class CaptionsLayer(LayerBase):
    """Word-timed captions from the two-reader transcript. Pages are derived at commit."""
    type: Literal["captions"] = "captions"
    role: Literal["content", "background", "brand", "decor", "text"] = "text"
    box: Box
    style: CaptionStyle
    mode: Literal["karaoke", "plain"] = "plain"
    max_chars: int = 24
    max_lines: int = 2
    pages: list[dict[str, Any]] | None = None   # filled by the compiler: {from, to, copy_id, lines:[[{text, from, to}]]}


Layer = Annotated[Union[VideoLayer, ImageLayer, ClipLayer, ShapeLayer, TextLayer, CaptionsLayer],
                  Field(discriminator="type")]


class CopyEntry(BaseModel):
    """A string that will be typeset. Minted by copy_add (or derived for captions); never hand-written."""
    id: str
    text: str
    role: Literal["title", "caption", "label", "callout", "endcard", "reference"]
    source: dict[str, Any]              # {kind: asr|ocr|authored|approved, ...refs}
    readers: dict[str, str] = Field(default_factory=dict)
    agree: bool | None = None
    regulated: bool = False
    status: Literal["unverified", "confirmed", "approved"] = "unverified"
    sha256: str = ""


class AudioSpec(BaseModel):
    fade_in_ms: int = 30
    fade_out_ms: int = 180
    seam_fade_ms: int = 25
    loudness_lufs: float = -14.0
    true_peak_dbtp: float = -1.0


class Canvas(BaseModel):
    schema_id: str = Field(SCHEMA, alias="schema")
    request: str
    output: str
    version: int = 0
    parent: int | None = None
    profile: str = "youtube_shorts"
    format: dict[str, int]              # width, height, fps
    time_map: list[Span]
    duration_frames: int | None = Field(default=None, ge=1, strict=True)
    layers: list[Layer] = Field(default_factory=list)
    copy_deck: list[CopyEntry] = Field(default_factory=list, alias="copy")
    audio: AudioSpec = Field(default_factory=AudioSpec)
    intent: str = ""                    # what this output is for, in the author's words
    declared: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @property
    def length(self) -> int:
        return sum(s.src_out - s.src_in for s in self.time_map) if self.time_map else (self.duration_frames or 0)

    @property
    def fps(self) -> int:
        return int(self.format["fps"])

    @property
    def size(self) -> tuple[int, int]:
        return int(self.format["width"]), int(self.format["height"])

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def copy_by_id(self) -> dict[str, CopyEntry]:
        return {c.id: c for c in self.copy_deck}

    def layer(self, layer_id: str):
        return next((l for l in self.layers if l.id == layer_id), None)
