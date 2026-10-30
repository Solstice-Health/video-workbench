"""Pinned originals, provider pixel payloads and arbitrary chat file intake."""
from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from vwb.workspace import Workspace, attachments
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-attachments")
    for sub in SUBDIRS:
        (workspace.root / sub).mkdir(parents=True, exist_ok=True)
    workspace.save(Request(id=workspace.id, message="Inspect files"))
    return workspace


def test_any_file_keeps_exact_original_and_separate_working_copy(ws, tmp_path):
    source = tmp_path / "unknown.customformat"
    source.write_bytes(b"\0\xffarbitrary binary")
    ref, = attachments.ingest(ws, [source])
    original = attachments.original_path(ws, ref)
    assert ref["kind"] == "download" and ref["bytes"] == source.stat().st_size
    assert original.stat().st_ino != source.stat().st_ino
    work = ws.work / ref["path"]
    assert work.stat().st_ino != original.stat().st_ino
    work.write_bytes(b"agent edited its copy")
    source.write_bytes(b"user changed local file")
    assert original.read_bytes() == b"\0\xffarbitrary binary"
    ws.add_chat("human", "", attachments=[ref])
    assert ws.load().chat[-1].attachments == [ref]
    assert "base64" not in ws.request_path.read_text()
    assert attachments.image_payload(ws, ref) is None


def test_payload_uses_pinned_pixels_and_bounds_resolution(ws, tmp_path):
    source = tmp_path / "image.png"
    Image.new("RGBA", (4096, 1024), "red").save(source)
    ref, = attachments.ingest(ws, [source])
    Image.new("RGB", (10, 10), "blue").save(ws.work / ref["path"])
    payload = attachments.image_payload(ws, ref)
    image = Image.open(io.BytesIO(base64.b64decode(payload["base64"])))
    assert image.size == (2048, 512)
    assert image.getpixel((100, 100))[:3] == (255, 0, 0)
    assert len(base64.b64decode(payload["base64"])) <= attachments.MAX_PAYLOAD_BYTES
    assert Path(payload["path"]).is_file()
    assert attachments.image_payload(ws, ref)["sha256"] == payload["sha256"]


def test_ref_forgery_and_symlink_working_directory_rejected(ws, tmp_path):
    source = tmp_path / "instructions.txt"
    source.write_text("Contents")
    ref, = attachments.ingest(ws, [source])
    for forged in [{**ref, "path": "../../secret"}, {**ref, "sha256": "0" * 64}]:
        with pytest.raises(ValueError, match="does not match"):
            attachments.validate_refs(ws, [forged])
    outside = tmp_path / "outside"
    outside.mkdir()
    import shutil
    shutil.rmtree(ws.work / "attachments")
    (ws.work / "attachments").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        attachments.ingest(ws, [source])
    assert not list(outside.iterdir())


def test_empty_files_and_misnamed_images_are_not_falsely_visual(ws, tmp_path):
    empty = tmp_path / "empty"
    empty.touch()
    fake = tmp_path / "picture.png"
    fake.write_bytes(b"\x00Not a PNG")
    refs = attachments.ingest(ws, [empty, fake])
    assert all(ref["kind"] == "download" for ref in refs)
    assert refs[0]["bytes"] == 0


def test_original_tampering_is_not_sent_to_provider(ws, tmp_path):
    source = tmp_path / "sample.png"
    Image.new("RGB", (20, 10), "red").save(source)
    ref, = attachments.ingest(ws, [source])
    path = attachments.original_path(ws, ref)
    path.chmod(0o644)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        attachments.image_payload(ws, ref)


def test_orchestrator_keeps_all_refs_without_eager_video_indexing(ws, tmp_path, monkeypatch):
    from vwb import orchestrator
    video = tmp_path / "My source.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom-fake-video-for-intake")
    document = tmp_path / "notes.md"
    document.write_text("# Notes")
    indexed = []
    messages = []
    monkeypatch.setattr(orchestrator, "build_index", lambda workspace, **kw: indexed.append(workspace.id))
    monkeypatch.setattr(orchestrator.job_lead, "run", lambda workspace, message, **kw: messages.append(message) or "done")
    monkeypatch.setattr(orchestrator, "ensure_review", lambda workspace: [])
    monkeypatch.setattr(orchestrator.page, "build", lambda workspace: workspace.review / "index.html")
    orchestrator.chat("", attach=[document, video], request_id=str(ws.root))
    req = ws.load()
    assert len(req.sources) == 1 and Path(req.sources[0].original_path).name == video.name
    assert len(req.chat[-1].attachments) == 2 and indexed == []
    refs = req.chat[-1].attachments
    orchestrator.chat("Follow up", request_id=str(ws.root), attachment_refs=refs, skip_index=True)
    assert len(ws.load().sources) == 1
    assert ws.load().chat[-1].attachments == refs
    assert [message.split("\n\n[harness]", 1)[0] for message in messages] == ["", "Follow up"]
    assert all(req.sources[0].ref in message for message in messages)


def test_palette_transparency_survives_provider_payload(ws, tmp_path):
    image = Image.new("P", (40, 20))
    image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    image.info["transparency"] = 0
    source = tmp_path / "palette.png"
    image.save(source)
    ref, = attachments.ingest(ws, [source])
    payload = attachments.image_payload(ws, ref)
    decoded = Image.open(io.BytesIO(base64.b64decode(payload["base64"])))
    assert decoded.mode == "RGBA" and decoded.getpixel((0, 0))[3] == 0


def test_fake_video_does_not_trigger_source_indexing(ws, tmp_path):
    source = tmp_path / "not-a-video.mp4"
    source.write_text("These are notes, not a video.")
    refs = attachments.ingest(ws, [source])
    assert attachments.source_paths(ws, refs) == []


def test_payload_cache_reuses_bytes_and_rebuilds_corrupt_sidecar(ws, tmp_path, monkeypatch):
    source = tmp_path / "cached.png"
    Image.new("RGB", (32, 16), "orange").save(source)
    ref, = attachments.ingest(ws, [source])
    first = attachments.image_payload(ws, ref)
    open_image = Image.open
    def unexpected_decode(*args, **kwargs):
        raise AssertionError("The valid cached payload must not decode again")
    monkeypatch.setattr(Image, "open", unexpected_decode)
    assert attachments.image_payload(ws, ref) == first
    monkeypatch.setattr(Image, "open", open_image)
    Path(first["path"]).write_bytes(b"corrupt cache")
    assert attachments.image_payload(ws, ref) == first


def test_large_encoded_image_stays_attachable_without_vision_payload(ws, tmp_path, monkeypatch):
    source = tmp_path / "large.png"
    Image.new("RGB", (32, 16), "orange").save(source)
    monkeypatch.setattr(attachments, "MAX_IMAGE_SOURCE_BYTES", 16)
    ref, = attachments.ingest(ws, [source])
    assert ref["kind"] == "download" and "encoded-size" in ref["preview_note"]
    assert attachments.original_path(ws, ref).read_bytes() == source.read_bytes()
    assert attachments.image_payload(ws, ref) is None
