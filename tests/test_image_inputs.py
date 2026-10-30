"""Direct original image inputs, safe masks, and index-free frame pixels."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess

import numpy as np
import pytest
from PIL import Image

from vwb.gen.refs import resolve
from vwb.media import images
from vwb.tools import gen_tools, inspection_tools
from vwb.tools.base import Registry, ToolContext
from vwb.workspace import Workspace, attachments
from vwb.workspace.media import register_work_file
from vwb.workspace.registry import Registry as Assets
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ctx(tmp_path):
    ws = Workspace(tmp_path / "r-images")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True)
    ws.save(Request(id=ws.id, message="Edit an image"))
    pixels = np.arange(40 * 32 * 4, dtype=np.uint8).reshape(32, 40, 4)
    Image.fromarray(pixels).save(ws.work / "input.png")
    return ToolContext(ws, "test", "job_lead", None)


def invoke(ctx, name, args):
    return Registry(gen_tools.TOOLS + inspection_tools.TOOLS).call(ctx, name, args)


def inpaint_stub(gateway, rgba, mask, prompt, **kwargs):
    assert gateway is None
    rgb = rgba[..., :3].copy()
    rgb[mask > 0] = [10, 20, 30]
    return {"rgb": rgb, "model": "offline-fixture", "seam": {"mae": 0}, "mask_px": int(mask.sum()),
            "text_in_mask_after": [], "window": [0, 0, rgba.shape[1], rgba.shape[0]]}


@pytest.mark.parametrize("style", ["bare", "work", "public", "attachment", "slot"])
def test_inpaint_accepts_original_images_without_video_or_index(ctx, monkeypatch, style):
    path = ctx.ws.work / "input.png"
    ref = {"bare": "input.png", "work": "work:input.png", "public": "work/input.png"}.get(style)
    if style == "attachment":
        row, = attachments.ingest(ctx.ws, [path])
        ref = f"attachment:{row['id']}"
    if style == "slot":
        ref = Assets(ctx.ws.assets).register(path, slot="input", kind="image", receipt="human.upload", derived_from=[])
    original = np.asarray(Image.open(path).convert("RGBA"))
    monkeypatch.setattr(gen_tools.sb, "inpaint", inpaint_stub)
    result = invoke(ctx, "sunburst_inpaint", {"source": ref, "mask": {"boxes": [[4, 6, 8, 10]], "pad": 0},
                    "prompt": "Remove the caption", "exact_mask": True, "slot": "clean"})
    assert not result.is_error, result.text
    saved = Assets(ctx.ws.assets).get(result.data["ref"])
    actual = np.asarray(Image.open(saved["path"]))
    mask = np.zeros(original.shape[:2], bool)
    mask[6:16, 4:12] = True
    assert np.array_equal(actual[~mask], original[~mask])
    assert np.array_equal(actual[..., 3], original[..., 3])
    lineage = json.loads(saved["derived_from"])[0]
    assert lineage["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not ctx.ws.load().sources and not list(ctx.ws.index.rglob("*.sqlite"))


def test_original_attachment_is_distinct_from_mutable_work_copy(ctx):
    row, = attachments.ingest(ctx.ws, [ctx.ws.work / "input.png"])
    Image.new("RGBA", (9, 7), "red").save(ctx.ws.work / row["path"])
    original = resolve(ctx.ws, f"attachment:{row['id']}")
    current = resolve(ctx.ws, row["path"])
    assert original.rgba.shape == (32, 40, 4) and current.rgba.shape == (7, 9, 4)
    assert original.lineage["sha256"] == row["sha256"] != current.lineage["sha256"]


@pytest.mark.parametrize("source_type", ["attachment", "slot"])
def test_pinned_image_integrity_checked_before_decode(ctx, source_type):
    if source_type == "attachment":
        row, = attachments.ingest(ctx.ws, [ctx.ws.work / "input.png"])
        ref = f"attachment:{row['id']}"
        path = ctx.ws.root / ".attachments" / row["id"] / "original"
    else:
        ref = Assets(ctx.ws.assets).register(ctx.ws.work / "input.png", slot="input", kind="image",
                                           receipt="human.upload", derived_from=[])
        from pathlib import Path
        path = Path(Assets(ctx.ws.assets).get(ref)["path"])
    path.chmod(0o600)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        resolve(ctx.ws, ref)


def test_inspection_and_generation_share_oriented_fractional_crop_coordinates(ctx):
    with Image.open(ctx.ws.work / "input.png") as im:
        exif = im.getexif()
        exif[274] = 6
        im.convert("RGB").save(ctx.ws.work / "rotated.jpg", exif=exif)
    row, = attachments.ingest(ctx.ws, [ctx.ws.work / "rotated.jpg"])
    reference = f"attachment:{row['id']}"
    crop = [2.4, 3.6, 15.2, 19.3]
    edited = resolve(ctx.ws, reference, crop)
    seen = invoke(ctx, "image_inspect", {"source": reference, "crop": crop})
    assert not seen.is_error, seen.text
    assert seen.data["source_size"] == [32, 40]
    assert seen.data["crop"] == edited.lineage["crop"] == [2, 3, 16, 20]
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(seen.images[0]))), edited.rgba)


@pytest.mark.parametrize("reference", ["../input.png", "/etc/passwd", ".hidden/input.png", "link.png", "folder/input.png", "pipe.png"])
def test_direct_image_inputs_reject_unsafe_paths(ctx, reference):
    (ctx.ws.work / "link.png").symlink_to(ctx.ws.work / "input.png")
    (ctx.ws.work / "folder").symlink_to(ctx.ws.work, target_is_directory=True)
    os.mkfifo(ctx.ws.work / "pipe.png")
    with pytest.raises((ValueError, OSError)):
        resolve(ctx.ws, reference)


def test_shared_project_work_is_used(ctx, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "work").mkdir(parents=True)
    Image.new("RGB", (12, 14), "blue").save(project / "work" / "input.png")
    monkeypatch.setattr(Workspace, "project_root", property(lambda _: project))
    assert resolve(ctx.ws, "input.png").rgba.shape == (14, 12, 4)


def test_bound_before_decoding_and_reject_animated_images(ctx, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(images, "MAX_SOURCE_BYTES", 1)
        with pytest.raises(ValueError, match="50 MiB"):
            resolve(ctx.ws, "input.png")
    with monkeypatch.context() as m:
        m.setattr(images, "MAX_SOURCE_PIXELS", 1)
        with pytest.raises(ValueError, match="megapixels"):
            resolve(ctx.ws, "input.png")
    Image.new("RGB", (8, 8), "red").save(ctx.ws.work / "animated.gif", save_all=True,
                                         append_images=[Image.new("RGB", (8, 8), "blue")], duration=100)
    with pytest.raises(ValueError, match="single still"):
        resolve(ctx.ws, "animated.gif")


@pytest.mark.parametrize("mask", [
    {"boxes": [[-30, 6, 10, 10]], "pad": 0}, {"boxes": [[5, -30, 10, 10]], "pad": 0},
    {"boxes": [[100, 5, 10, 10]]}, {"boxes": [[0, 0, -1, 10]]}, {"boxes": [[0, 0, float("nan"), 10]]},
    {"boxes": [[0, 0, 10, 10]], "pad": -1}, {"boxes": [[0, 0, 10, 10]], "pad": float("inf")},
])
def test_invalid_mask_never_reaches_model(ctx, monkeypatch, mask):
    monkeypatch.setattr(gen_tools.sb, "inpaint", lambda *a, **kw: pytest.fail("invalid mask reached provider"))
    result = invoke(ctx, "sunburst_inpaint", {"source": "input.png", "mask": mask, "prompt": "Clean"})
    assert result.is_error
    assert not Assets(ctx.ws.assets).list()


@pytest.mark.parametrize("field", ["text_tracks", "blocks"])
def test_cropped_index_mask_rejected_before_generation(ctx, field):
    result = invoke(ctx, "sunburst_inpaint", {"source": "input.png", "crop": [1, 2, 20, 20],
                      "mask": {field: [0]}, "prompt": "Clean"})
    assert result.is_error and "source-frame pixels" in result.text


def test_mask_image_requires_real_alpha(ctx):
    Image.new("RGB", (40, 32), "black").save(ctx.ws.work / "mask.png")
    with pytest.raises(ValueError, match="alpha channel"):
        gen_tools._mask_from(ctx, {"mask": {"asset": "mask.png"}}, "input.png", (32, 40))
    alpha = Image.new("RGBA", (40, 32), (0, 0, 0, 0))
    alpha.putpixel((5, 8), (0, 0, 0, 255))
    alpha.save(ctx.ws.work / "mask.png")
    mask = gen_tools._mask_from(ctx, {"mask": {"asset": "mask.png"}}, "input.png", (32, 40))
    assert mask.sum() == 1 and mask[8, 5]


def test_real_mkv_frame_reference_requires_no_index(ctx):
    path = ctx.ws.work / "still.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:r=30",
                    "-t", "2", "-c:v", "ffv1", str(path)], check=True, capture_output=True)
    src = register_work_file(ctx.ws, "still.mkv")
    by_frame = resolve(ctx.ws, f"source:{src.ref}#f=30")
    by_time = resolve(ctx.ws, f"source:{src.ref}#t=1")
    assert np.array_equal(by_frame.rgba, by_time.rgba)
    assert by_frame.lineage["frame"] == by_time.lineage["frame"] == 30
    assert by_frame.rgba[0, 0, 0] > 240
    assert not list(ctx.ws.index.rglob("*.sqlite"))
    for coordinate in ("f=-1", "t=nan", "f=1.5", "f=60"):
        with pytest.raises((ValueError, IndexError)):
            resolve(ctx.ws, f"source:{src.ref}#{coordinate}")


def test_png_registration_error_explains_direct_input(ctx):
    with pytest.raises(ValueError, match="No video conversion, registration or index"):
        register_work_file(ctx.ws, "input.png")


def test_unplaced_extraction_returns_saved_asset_even_when_text_unverified(ctx, monkeypatch):
    rgba = resolve(ctx.ws, "input.png").rgba
    monkeypatch.setattr(gen_tools.sb, "extract", lambda *a, **kw: {
        "rgba": rgba, "region": rgba, "cut_full": rgba, "placed": None, "alpha": {},
        "fidelity": {"registered": False}, "model": "offline-fixture", "prompt": "Extract"})
    monkeypatch.setattr(gen_tools, "_source_text", lambda _: ["Caption"])
    monkeypatch.setattr(gen_tools.qa, "faces", lambda _: 0)
    result = invoke(ctx, "sunburst_extract", {"source": "input.png", "subject": "Logo", "slot": "cut"})
    assert not result.is_error, result.text
    assert result.data["ref"] == "slot:cut@1" and "unverified" in result.data["notes"][-1]
    assert len(Assets(ctx.ws.assets).list()) == 1


def test_zero_decoded_samples_cannot_be_saved_as_successful_vision_index(monkeypatch):
    from types import SimpleNamespace

    from vwb.index import build
    builder = object.__new__(build.IndexBuilder)
    builder.fps = 30
    builder.info = {"width": 64, "height": 48}
    builder.src = SimpleNamespace(path="not-read")
    builder._shots = []
    monkeypatch.setattr(build, "sample_frames", lambda *a: iter([]))
    with pytest.raises(ValueError, match="no samples"):
        builder.stage_vision()
