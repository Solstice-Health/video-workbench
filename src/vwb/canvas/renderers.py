"""Trusted canvas backends. A document chooses one renderer, never executable code.

Keep the legacy Remotion pin stable: existing immutable canvases and plate caches
must retain their meaning. New backends have separate content-addressed caches.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vwb.canvas.model import Canvas


@dataclass(frozen=True)
class Renderer:
    id: str
    label: str
    pin: str
    description: str
    plate_layers: tuple[str, ...] = ("text", "captions", "shape")


RENDERERS = {
    row.id: row for row in (
        Renderer("remotion", "Remotion", "remotion@4.0.534:Scene@4",
                 "Existing React video renderer; full canvas layer support."),
        Renderer("browser", "React / HTML + SVG", "browser-scene@1",
                 "Frame-driven HTML typography and SVG graphics in headless Chromium."),
        Renderer("gsap", "React / HTML + GSAP", "gsap-scene@1",
                 "HTML/SVG scene with paused GSAP timelines sampled at explicit frames."),
        Renderer("canvas2d", "Canvas 2D", "canvas2d-scene@1",
                 "Canvas 2D graphics. Shape plates only; use HTML/SVG for text or captions.", ("shape",)),
    )
}
DEFAULT_RENDERER = "remotion"


def get_renderer(renderer: str = DEFAULT_RENDERER) -> Renderer:
    try:
        return RENDERERS[renderer]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown canvas renderer {renderer!r}; choose {', '.join(RENDERERS)}") from None


def catalog() -> dict:
    return {"default": DEFAULT_RENDERER, "renderers": [asdict(row) for row in RENDERERS.values()],
            "selection": "Choose one renderer per output. Multiple composer choices are preferences for the agent.",
            "execution": "Trusted adapters accept canvas data only; arbitrary source code uses the existing sandbox."}


def renderer_problems(canvas: Canvas) -> list[tuple[str, str]]:
    renderer = get_renderer(canvas.renderer)
    problems = []
    if canvas.renderer_pin and canvas.renderer_pin != renderer.pin:
        problems.append(("renderer_pin", f"This canvas pins {canvas.renderer_pin}, but the installed {renderer.id} "
                         f"adapter is {renderer.pin}. Use a checkout with that adapter, or explicitly switch the working canvas."))
    for layer in canvas.layers:
        if layer.type in {"text", "captions", "shape"} and layer.type not in renderer.plate_layers:
            problems.append((layer.id, f"{renderer.label} does not support {layer.type} plates. Choose browser, gsap or remotion; "
                             "the renderer will not flatten or drop this layer."))
    return problems


def validate_renderer(canvas: Canvas) -> Renderer:
    problems = renderer_problems(canvas)
    if problems:
        raise ValueError("; ".join(f"{where}: {why}" for where, why in problems))
    return get_renderer(canvas.renderer)


def props_renderer(props: dict) -> Renderer:
    return get_renderer(props.get("__renderer", DEFAULT_RENDERER))
