from __future__ import annotations

import base64
import io
import subprocess

import pypdfium2 as pdfium
import pytest
from PIL import Image

from vwb.web import pdf_preview
from vwb.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    workspace = Workspace(tmp_path / "r-pdf")
    workspace.work.mkdir(parents=True)
    with pdfium.PdfDocument.new() as doc:
        doc.new_page(612, 792).close()
        doc.new_page(2000, 1000).close()
        doc.save(workspace.work / "document.pdf")
    return workspace


def test_real_renderer_multiple_pages_and_bounded_dimensions(ws):
    # Exercise a descriptor whose parent buffered read has read ahead.
    with (ws.work / "document.pdf").open("ab") as handle:
        handle.write(b"\n" * 8192)
    first = pdf_preview.render(ws, "work/document.pdf")
    second = pdf_preview.render(ws, "work/document.pdf", 2)
    assert (first["page"], first["pages"]) == (1, 2)
    assert second["width"] == 1600 and second["height"] == 800
    with Image.open(io.BytesIO(base64.b64decode(second["image"].split(",", 1)[1]))) as image:
        assert image.format == "PNG" and image.size == (1600, 800)
    with pytest.raises(ValueError, match="does not exist"):
        pdf_preview.render(ws, "work/document.pdf", 3)


def test_renderer_rejects_private_paths_symlinks_invalid_documents(ws):
    (ws.work / "link.pdf").symlink_to(ws.work / "document.pdf")
    (ws.work / "fake.pdf").write_text("<script>alert('not a PDF')</script>")
    for path in ("memory/private.pdf", "work/link.pdf"):
        with pytest.raises(PermissionError):
            pdf_preview.render(ws, path)
    with pytest.raises(ValueError, match="not a PDF"):
        pdf_preview.render(ws, "work/fake.pdf")
    (ws.work / "bad.pdf").write_bytes(b"%PDF-1.7\nbroken")
    with pytest.raises(ValueError, match="could not be previewed"):
        pdf_preview.render(ws, "work/bad.pdf")


def test_renderer_timeout_releases_concurrency_slot(ws, monkeypatch):
    def timeout(*args, **kwargs):
        assert "pass_fds" in kwargs and kwargs["timeout"] == 20
        assert kwargs["env"] == {"PYTHONIOENCODING": "utf-8"}
        raise subprocess.TimeoutExpired(args[0], 20)

    monkeypatch.setattr(subprocess, "run", timeout)
    for _ in range(3):
        with pytest.raises(ValueError, match="took too long"):
            pdf_preview.render(ws, "work/document.pdf")


def test_renderer_rejects_oversized_input_before_process_launch(ws, monkeypatch):
    monkeypatch.setattr(pdf_preview, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="64 MiB"):
        pdf_preview.render(ws, "work/document.pdf")


@pytest.mark.parametrize("page", [0, -1, True, "1", 10001])
def test_renderer_page_validation(ws, page):
    with pytest.raises(ValueError, match="Choose a PDF page"):
        pdf_preview.render(ws, "work/document.pdf", page)
