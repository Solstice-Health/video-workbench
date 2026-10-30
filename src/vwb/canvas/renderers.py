"""Authoring targets and trusted canvas backends.

Native projects are separate authoring targets, never canvas renderer adapters.
A canvas document chooses one renderer, never executable code.

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
        Renderer("browser", "React / HTML + SVG", "browser-scene@1:playwright-core@1.64.1:esbuild@0.28.2",
                 "Frame-driven HTML typography and SVG graphics in headless Chromium."),
        Renderer("gsap", "React / HTML + GSAP", "gsap-scene@1:gsap@3.15.0:playwright-core@1.64.1:esbuild@0.28.2",
                 "HTML/SVG scene with paused GSAP timelines sampled at explicit frames."),
        Renderer("canvas2d", "Canvas 2D", "canvas2d-scene@1:playwright-core@1.64.1:esbuild@0.28.2",
                 "Canvas 2D graphics. Shape plates only; use HTML/SVG for text or captions.", ("shape",)),
    )
}
DEFAULT_RENDERER = "remotion"


def get_renderer(renderer: str = DEFAULT_RENDERER) -> Renderer:
    if renderer == "after_effects":
        raise ValueError("After Effects is a native project target, not a vwb.canvas/2 renderer. "
                         "Use after_effects_status, after_effects_run and after_effects_render; "
                         "import the rendered video with media_register. No automatic layer conversion is available.")
    try:
        return RENDERERS[renderer]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown canvas renderer {renderer!r}; choose {', '.join(RENDERERS)}") from None


def after_effects_target(status: dict | None = None) -> dict:
    """Describe the native route without probing the host or granting authority.

    A human-facing caller may supply settings_status(). Configuration readiness
    cannot attest Adobe licensing, scripting preferences or macOS Automation.
    """
    availability = {"status": "unknown", "reason": "Check after_effects_status for this host's installation and enablement."}
    if status is not None:
        if not status.get("supported"):
            availability = {"status": "unavailable", "reason": "The native After Effects bridge requires macOS."}
        elif not status.get("available"):
            availability = {"status": "unavailable", "reason": "After Effects was not found. Install it or choose its app path in Manage connections."}
        elif not status.get("enabled"):
            availability = {"status": "setup_required", "reason": "A person must enable After Effects automation in Plugins and MCPs."}
        elif status.get("pending_execution"):
            availability = {"status": "unavailable", "reason": status["pending_execution"].get("message", "An active or unresolved script blocks new authoring.")}
        elif not status.get("can_render"):
            availability = {"status": "setup_required", "reason": "Authoring is configured; select an installed aerender to render saved projects."}
        else:
            availability = {"status": "available", "reason": "Native automation is configured. Adobe licensing, script file access and macOS consent are verified only by execution."}
    return {
        "id": "after_effects", "label": "After Effects", "kind": "native_project",
        "description": "Author editable .aep projects with native JSX, render with aerender, and import the rendered video.",
        "scope": "Separate native project. No automatic vwb.canvas/2 conversion, renderer swap or embedded AE editor; layers and effects stay editable in After Effects.",
        "capability": "desktop.after_effects.execute",
        "required_tools": ["after_effects_status", "file_write", "after_effects_run", "after_effects_job", "after_effects_render"],
        "import_tools": ["media_register"],
        "supports_renderer_swap": False,
        "availability": availability,
        "workflow": [
            {"tool": "after_effects_status", "purpose": "Inspect host configuration; selection does not enable automation or bypass policy."},
            {"tool": "file_write", "purpose": "Write a .jsx script under work/. Use vwb.workPath/outputPath for host paths and return comp/template metadata in vwb.result."},
            {"tool": "after_effects_run", "purpose": "Author an isolated saved .aep by default; project_path opens a workspace project and saves a new copy."},
            {"tool": "after_effects_job", "purpose": "Wait for completed, inspect errors and retain project.path, result and receipt before rendering."},
            {"tool": "after_effects_render", "purpose": "Render project.path with a comp or render_queue_index and installed templates to a new workspace movie."},
            {"tool": "after_effects_job", "purpose": "Wait for completed and inspect output.path and its hash. Native output review remains not_evaluated."},
            {"tool": "media_register", "purpose": "Optionally import output.path as an immutable video source for inspection or a separately authored canvas. Import preserves rendered pixels, not AE layers/effects."},
        ],
    }


def catalog(*, after_effects_status: dict | None = None) -> dict:
    renderers = [asdict(row) for row in RENDERERS.values()]
    targets = [{**asdict(row), "kind": "canvas_renderer",
                "scope": ("Shapes only; no text or captions." if row.plate_layers == ("shape",) else
                          "Supported plates: " + ", ".join(row.plate_layers) + "."),
                "capability": "tool", "required_tools": ["canvas_new", "canvas_set_renderer", "canvas_check", "canvas_preview"],
                "supports_renderer_swap": True} for row in RENDERERS.values()]
    targets.append(after_effects_target(after_effects_status))
    return {"default": DEFAULT_RENDERER, "renderers": renderers, "targets": targets,
            "selection": "Choose an authoring target per output. Multiple composer choices are preferences; selecting a native target does not enable it.",
            "execution": "Trusted adapters accept canvas data only. Native targets use their own human-enabled tools and policy gates; other arbitrary code uses the sandbox."}


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
