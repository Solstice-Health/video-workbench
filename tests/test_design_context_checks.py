"""Concrete media/copy enforcement regressions, using isolated real workspaces."""
import hashlib
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from vwb.canvas.model import Canvas, CopyEntry
from vwb.design_context import checks, store
from vwb.util.hashing import sha256_file
from vwb.util.jsonio import write_json
from vwb.workspace import Workspace
from vwb.workspace.registry import Registry
from vwb.workspace.request import SUBDIRS, Request

COPY = "Keep all words."


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-checks")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True, exist_ok=True)
    workspace.save(Request(id=workspace.id, message="Use this synthetic context"))
    (workspace.work / "isi.txt").write_text(COPY)
    return workspace


def rule(kind, **kwargs):
    return {"id": "check", "kind": kind, "level": "required", "description": "Synthetic constraint", **kwargs}


def image_asset(ws, name="logo", color="blue"):
    path = ws.work / f"{name}.png"
    Image.new("RGB", (8, 8), color).save(path)
    ref = Registry(ws.assets).register(path, slot=name, kind="raster", receipt="human.upload", derived_from=[])
    return path, ref


def image_layer(ref, **extra):
    return {"type": "image", "id": "logo", "role": "brand", "asset": ref, "box": [100, 100, 200, 200], **extra}


def text_layer(**extra):
    return {"type": "text", "id": "isi", "copy_id": "isi", "box": [100, 100, 800, 500],
            "style": {"font": "Inter", "weight": 400, "size": 40, "min_size": 40, "color": "#FFFFFF"}, **extra}


def bound_canvas(ws, rules=(), assets=None, layers=(), timeline=()):
    sources = [{"id": "isi", "path": "isi.txt", "title": "Exact copy", "kind": "safety_information"}]
    sources += [{"id": name, "path": path.relative_to(ws.work).as_posix(), "title": name, "kind": "asset"}
                for name, path in (assets or {}).items()]
    write_json(ws.work / "context.json", {"brand": "Synthetic", "revision": "1", "sources": sources,
                                          "rules": list(rules)})
    pack = store.import_pack(ws, "context.json")["pack"]
    binding = store.bind(ws, "board", pack, {})
    return Canvas(request=ws.id, output="board", version=1, design_context=binding,
                  format={"width": 1080, "height": 1920, "fps": 30},
                  time_map=list(timeline), duration_frames=None if timeline else 30, layers=list(layers),
                  copy=[CopyEntry(id="isi", text=COPY, role="reference", source={"kind": "authored"})])


def assert_blocked(ws, canvas, message):
    result = checks.evaluate(ws, canvas)
    assert result["status"] == "blocked", result
    assert any(message in f["why"] for f in result["findings"]), result
    with pytest.raises(ValueError, match="Design context prevents rendering"):
        checks.assert_renderable(ws, canvas)


@pytest.mark.parametrize("reference", ["slot:logo", "logo@1", "slot:logo@0", "slot:logo@01", "slot:logo@-1"])
def test_bound_media_requires_explicit_revision_even_without_asset_rules(ws, reference):
    image_asset(ws)
    canvas = bound_canvas(ws, layers=[image_layer(reference)])
    assert_blocked(ws, canvas, "explicit slot:name@revision")
    canvas.design_context = None
    assert checks.assert_renderable(ws, canvas)["status"] == "not_configured"


def test_saved_revision_survives_new_slot_revision_and_rechecks_same_stat_tampering(ws):
    path, ref = image_asset(ws)
    canvas = bound_canvas(ws, [rule("asset_allowlist", sources=["logo"])], {"logo": path}, [image_layer(ref)])
    registry = Registry(ws.assets)
    other, _ = image_asset(ws, "other", "red")
    later = registry.register(other, slot="logo", kind="raster", receipt="human.upload", derived_from=[])
    assert later != ref
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"

    frozen = Path(registry.get(ref)["path"])
    original = frozen.stat()
    cached = sha256_file(frozen)  # Prime the existing size/mtime digest cache.
    data = frozen.read_bytes()
    frozen.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    os.utime(frozen, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert frozen.stat().st_size == original.st_size
    assert sha256_file(frozen) == cached
    assert_blocked(ws, canvas, "frozen SHA-256")


@pytest.mark.parametrize("attack", ["outside", "symlink", "parent_symlink", "traversal", "fifo"])
def test_asset_path_boundary_rejects_same_bytes_outside_root_or_nonregular_path(ws, attack):
    path, ref = image_asset(ws)
    canvas = bound_canvas(ws, assets={"logo": path}, layers=[image_layer(ref)])
    registry = Registry(ws.assets)
    row = registry.get(ref)
    frozen = Path(row["path"])
    outside = ws.work / "outside.png"
    outside.write_bytes(frozen.read_bytes())
    if attack == "outside":
        replacement = outside
    elif attack == "symlink":
        replacement = ws.assets / "link.png"
        replacement.symlink_to(outside)
    elif attack == "parent_symlink":
        directory = ws.assets / "linked"
        directory.symlink_to(ws.work, target_is_directory=True)
        replacement = directory / outside.name
    elif attack == "traversal":
        replacement = frozen.parent / ".." / frozen.parent.name / frozen.name
    else:
        replacement = ws.assets / "pipe"
        os.mkfifo(replacement)
    with registry._conn() as con:
        con.execute("UPDATE revisions SET path=? WHERE slot=? AND rev=?", (str(replacement), row["slot"], row["rev"]))
    assert_blocked(ws, canvas, "cannot verify frozen media")


@pytest.mark.parametrize("race", ["in_place", "replace"])
def test_hash_rejects_inflight_content_or_path_identity_change(ws, monkeypatch, race):
    _, ref = image_asset(ws)
    asset = Registry(ws.assets).get(ref)
    path = Path(asset["path"])
    original_open = store._open
    count = 0

    @contextmanager
    def racing_open(root, relative):
        nonlocal count
        count += 1
        if count == 2 and race == "replace":
            replacement = path.with_name("replacement.png")
            shutil.copy2(path, replacement)
            os.replace(replacement, path)
        with original_open(root, relative) as stream:
            if count == 1 and race == "in_place":
                class ChangingReader:
                    def fileno(self):
                        return stream.fileno()

                    def read(self, size):
                        data = stream.read(size)
                        if data:
                            before = path.stat()
                            path.write_bytes(b"x" * before.st_size)
                            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                        return data
                yield ChangingReader()
            else:
                yield stream

    monkeypatch.setattr(store, "_open", racing_open)
    with pytest.raises(ValueError, match="changed during verification"):
        checks._verified_hash(ws.assets, path, asset["sha256"])


@pytest.mark.parametrize(("enter", "exit_effect"), [
    ("type", "none"), ("wipe", "none"), ("fade", "none"), ("rise", "none"), ("pop", "none"),
    ("none", "fade"), ("none", "sink"),
])
def test_required_copy_rejects_entrance_and_exit_effects(ws, enter, exit_effect):
    layer = text_layer(animate={"enter": enter, "exit": exit_effect, "frames": 12})
    canvas = bound_canvas(ws, [rule("required_copy", sources=["isi"], min_frames=30)], layers=[layer])
    assert_blocked(ws, canvas, "animated")


def test_static_frozen_copy_passes_without_reading_mutable_copy_deck(ws):
    canvas = bound_canvas(ws, [rule("required_copy", sources=["isi"], min_frames=30)], layers=[text_layer()])
    write_json(ws.root / "copy/deck.json", {"isi": {**canvas.copy_by_id()["isi"].model_dump(), "text": "Changed"}})
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"
    canvas.copy_deck[0].text = "Changed"
    assert_blocked(ws, canvas, "exact source copy")


def test_unused_rectangle_asset_attribute_does_not_satisfy_required_asset(ws):
    path, ref = image_asset(ws)
    layer = {"type": "shape", "id": "rect", "shape": "rect", "asset": ref, "box": [100, 100, 200, 200],
             "fill": "#FFFFFF"}
    canvas = bound_canvas(ws, [rule("required_asset", sources=["logo"])], {"logo": path}, [layer])
    assert_blocked(ws, canvas, "required library asset is absent")
    # An irrelevant field is not a media dependency, even if it is invalid.
    canvas.layers[0].asset = "not-a-media-reference"
    assert not any(f["where"] == "design_context:media" for f in checks.evaluate(ws, canvas)["findings"])


def test_registered_svg_is_a_real_asset_dependency(ws):
    path = ws.work / "vector.json"
    write_json(path, {"viewbox": [10, 10], "paths": [{"d": "M0 0 L10 0 L10 10 Z", "fill": "#FFFFFF"}]})
    ref = Registry(ws.assets).register(path, slot="vector", kind="vector", receipt="asset.trace", derived_from=[])
    layer = {"type": "shape", "id": "vector", "shape": "svg", "asset": ref, "box": [100, 100, 200, 200]}
    canvas = bound_canvas(ws, [rule("required_asset", sources=["vector"])], {"vector": path}, [layer])
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"
    canvas.layers[0].asset = "slot:vector"
    assert_blocked(ws, canvas, "explicit slot:name@revision")


@pytest.fixture(scope="module")
def media_files(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("Real audio/video dependency tests require ffmpeg")
    directory = tmp_path_factory.mktemp("context-media")
    paths = []
    for color in ("blue", "red"):
        path = directory / f"{color}.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-f", "lavfi", "-i", f"color=c={color}:s=32x32:r=30",
                        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000", "-t", "1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                       check=True, capture_output=True, timeout=30)
        paths.append(path)
    return paths


def add_media(ws, path, name="src1"):
    selected = ws.work / f"{name}.mp4"
    shutil.copyfile(path, selected)
    source = ws.add_source(selected, name)
    ws.update(sources=[*ws.load().sources, source])
    return selected, source


def time_map(source):
    return [{"media": source.ref, "src_in": 0, "src_out": 30}]


def test_unscoped_allowlist_checks_timeline_audio_without_a_video_layer(ws, media_files):
    _, source = add_media(ws, media_files[0])
    logo, ref = image_asset(ws)
    canvas = bound_canvas(ws, [rule("asset_allowlist", sources=["logo"])], {"logo": logo},
                          [image_layer(ref)], time_map(source))
    assert_blocked(ws, canvas, "Timeline audio source")


def test_role_scoped_allowlist_is_visual_only_but_still_rechecks_audio_integrity(ws, media_files):
    _, source = add_media(ws, media_files[0])
    logo, ref = image_asset(ws)
    canvas = bound_canvas(ws, [rule("asset_allowlist", sources=["logo"], roles=["brand"])], {"logo": logo},
                          [image_layer(ref)], time_map(source))
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"
    frozen = Path(source.path)
    before = frozen.stat()
    cached = sha256_file(frozen)
    data = frozen.read_bytes()
    frozen.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    os.utime(frozen, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert sha256_file(frozen) == cached
    assert_blocked(ws, canvas, "frozen SHA-256")


@pytest.mark.parametrize("attack", ["outside", "symlink", "floating_reference"])
def test_timeline_source_is_confined_and_explicit(ws, media_files, attack):
    selected, source = add_media(ws, media_files[0])
    canvas = bound_canvas(ws, timeline=time_map(source))
    if attack == "outside":
        source.path = str(selected)  # Same bytes, outside the immutable media root.
        ws.update(sources=[source])
    elif attack == "symlink":
        frozen = Path(source.path)
        frozen.unlink()
        frozen.symlink_to(selected)
    else:
        canvas.time_map[0].media = source.media_id
    assert_blocked(ws, canvas, "cannot verify frozen media")


def test_video_source_not_used_by_time_map_cannot_satisfy_required_asset(ws, media_files):
    _, active = add_media(ws, media_files[0])
    unused_path, unused = add_media(ws, media_files[1], "src2")
    layer = {"type": "video", "id": "unused", "source": unused.ref, "role": "brand", "box": [0, 0, 1080, 1920]}
    canvas = bound_canvas(ws, [rule("required_asset", sources=["unused"], roles=["brand"])],
                          {"unused": unused_path}, [layer], time_map(active))
    assert_blocked(ws, canvas, "required library asset is absent")


def test_video_layer_must_overlap_its_source_time_map_interval(ws, media_files):
    path, first = add_media(ws, media_files[0])
    _, second = add_media(ws, media_files[1], "src2")
    timeline = time_map(first) + time_map(second)
    layer = {"type": "video", "id": "first", "source": first.ref, "span": [30, 60],
             "role": "brand", "box": [0, 0, 1080, 1920]}
    canvas = bound_canvas(ws, [rule("required_asset", sources=["first"], roles=["brand"])],
                          {"first": path}, [layer], timeline)
    assert_blocked(ws, canvas, "required library asset is absent")
    canvas.layers[0].span = [0, 30]
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"


def test_clip_uses_its_registered_revision_without_a_source_time_map(ws, media_files):
    path = ws.work / "clip.mp4"
    shutil.copyfile(media_files[0], path)
    ref = Registry(ws.assets).register(path, slot="clip", kind="video", receipt="human.upload", derived_from=[])
    layer = {"type": "clip", "id": "clip", "asset": ref, "box": [0, 0, 1080, 1920]}
    canvas = bound_canvas(ws, [rule("required_asset", sources=["clip"])], {"clip": path}, [layer])
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"
    canvas.layers[0].asset = "slot:clip"
    assert_blocked(ws, canvas, "explicit slot:name@revision")


@pytest.mark.parametrize("color", ["#fff", "#fFfF", "#ffffff", "#FFFFFFff", " #FFF "])
def test_palette_compares_equivalent_supported_css_hex_colors(ws, color):
    layer = text_layer()
    layer["style"]["color"] = color
    canvas = bound_canvas(ws, [rule("palette", values=["#FFFFFF"])], layers=[layer])
    assert checks.assert_renderable(ws, canvas)["status"] == "structural_checks_passed"


@pytest.mark.parametrize("color", ["red", "rgb(255,0,0)", "var(--brand)", "currentColor", "#GGGGGG", "#f00", "#fff0"])
def test_palette_never_silently_skips_unknown_or_disallowed_colors(ws, color):
    layer = text_layer()
    layer["style"]["color"] = color
    canvas = bound_canvas(ws, [rule("palette", values=["#FFFFFF"])], layers=[layer])
    assert_blocked(ws, canvas, "palette" if color in {"#f00", "#fff0"} else "Unsupported color")


def test_palette_checks_animated_colors_and_gradient_stops(ws):
    layer = text_layer()
    layer["style"]["color"] = [{"f": 0, "v": "#fff"}, {"f": 20, "v": "red"}]
    canvas = bound_canvas(ws, [rule("palette", values=["#FFFFFF"])], layers=[layer])
    assert_blocked(ws, canvas, "Unsupported color")
    from vwb.canvas.model import ShapeLayer
    canvas.layers = [ShapeLayer(id="gradient", shape="rect", box=[0, 0, 1080, 1920], gradient=["#fff", "red"])]
    assert_blocked(ws, canvas, "Unsupported color")


def test_render_preflight_keeps_advisory_failures_nonblocking(ws):
    layer = text_layer()
    layer["style"]["color"] = "red"
    canvas = bound_canvas(ws, [rule("palette", values=["#FFFFFF"], level="advisory")], layers=[layer])
    report = checks.assert_renderable(ws, canvas)
    assert report["status"] == "structural_checks_passed"
    assert report["findings"] and all(f["severity"] == "warn" for f in report["findings"])


def test_render_preflight_turns_unreadable_pack_into_value_error(ws):
    canvas = bound_canvas(ws)
    canvas.design_context["pack"] = hashlib.sha256(b"missing pack").hexdigest()
    with pytest.raises(ValueError, match="Cannot verify design context before rendering"):
        checks.assert_renderable(ws, canvas)
