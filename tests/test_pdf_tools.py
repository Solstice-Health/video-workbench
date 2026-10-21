"""Real PDFs through the process boundary, plus input/resource contract tests."""
from __future__ import annotations

import io
import subprocess
from contextlib import closing
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from vwb.documents import pdf
from vwb.tools import pdf_tools
from vwb.tools.base import Registry, ToolContext


def text_pdf(path):
    # Minimal standards-compliant fixture, including metadata and a bookmark.
    # No authoring dependency or production document is needed for this test.
    first = b"BT /F1 20 Tf 40 250 Td (Quarterly report) Tj 0 -40 Td (Revenue 2026: 42 million. Revenue increased.) Tj ET"
    second = b"BT /F1 14 Tf 20 170 Td (Item      Amount) Tj 0 -30 Td (Example    42) Tj 0 -30 Td (Please verify every number visually.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /Outlines 9 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R 11 0 R] /Count 3 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(first)).encode() + b" >>\nstream\n" + first + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        b"<< /Length " + str(len(second)).encode() + b" >>\nstream\n" + second + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (PDF tool fixture) /Author (Workbench tests) >>",
        b"<< /Type /Outlines /First 10 0 R /Last 10 0 R /Count 1 >>",
        b"<< /Title (Quarterly results) /Parent 9 0 R /Dest [3 0 R /Fit] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>",
    ]
    data = b"%PDF-1.7\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    data += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 8 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(data)


@pytest.fixture
def ctx(tmp_path):
    root = tmp_path / "request"
    ws = SimpleNamespace(root=root, work=root / "work", ledger=root / "ledger")
    ws.work.mkdir(parents=True)
    ws.ledger.mkdir()
    folder = ws.work / "attachments" / "example"
    folder.mkdir(parents=True)
    text_pdf(folder / "report.pdf")
    return ToolContext(ws=ws, agent="pdf-test", role="job_lead", gateway=None)


SOURCE = "attachments/example/report.pdf"


def call(ctx, name, args=None):
    return Registry(pdf_tools.TOOLS).call(ctx, name, {"source": SOURCE, **(args or {})})


def test_real_info_metadata_bookmarks_and_page_facts(ctx):
    result = call(ctx, "pdf_info")
    assert not result.is_error, result.text
    assert result.data["pages"] == 3
    assert result.data["metadata"]["Title"] == "PDF tool fixture"
    assert result.data["outline"] == [{"title": "Quarterly results", "level": 0, "page": 1}]
    first, second, blank = result.data["page_details"]
    assert first["size"] == [400, 300] and second["size"] == [300, 200]
    assert first["text_status"] == "embedded_text"
    assert blank["text_status"] == "no_extractable_text"
    assert blank["needs_visual_reading"]
    assert len(result.data["sha256"]) == 64
    assert result.data["untrusted_document_content"] is True
    assert (ctx.ws.ledger / "tools.jsonl").exists()


def test_read_paginated_text_and_continuations(ctx):
    first = call(ctx, "pdf_read", {"page_count": 1})
    assert not first.is_error, first.text
    assert "Quarterly report" in first.data["page_details"][0]["text"]
    assert first.data["next_cursor"] == {"start_page": 2, "start_char": 0}
    second = call(ctx, "pdf_read", first.data["next_cursor"] | {"page_count": 1})
    assert "Item" in second.data["page_details"][0]["text"]
    assert "Amount" in second.data["page_details"][0]["text"]
    assert second.data["page_details"][0]["text_source"] == "embedded"
    assert second.data["sha256"] == first.data["sha256"]


def test_read_output_budget_returns_cursor_without_losing_content(ctx):
    # 100-character budget ends on page 2, whose continuation uses native char indices.
    whole = call(ctx, "pdf_read").data
    first = call(ctx, "pdf_read", {"max_chars": 100}).data
    assert first["next_cursor"] is not None
    second = call(ctx, "pdf_read", first["next_cursor"]).data
    first_text = "".join(page["text"] for page in first["page_details"])
    second_text = "".join(page["text"] for page in second["page_details"])
    assert first_text + second_text == "".join(page["text"] for page in whole["page_details"])


def test_search_is_literal_bounded_and_continuable(ctx):
    first = call(ctx, "pdf_search", {"query": "revenue", "max_results": 1})
    assert not first.is_error, first.text
    assert len(first.data["matches"]) == 1 and first.data["matches_limited"]
    assert first.data["matches"][0]["page"] == 1
    second = call(ctx, "pdf_search", {"query": "revenue", **first.data["next_cursor"]})
    assert len(second.data["matches"]) == 1
    assert second.data["matches"][0]["start_char"] > first.data["matches"][0]["start_char"]
    assert not call(ctx, "pdf_search", {"query": "Revenue.*"}).data["matches"]
    assert not call(ctx, "pdf_search", {"query": "revenue", "match_case": True}).data["matches"]
    partial = call(ctx, "pdf_search", {"query": "absent", "page_count": 1}).data
    assert partial["range"] == [1, 1] and partial["next_cursor"]["start_page"] == 2


def test_render_multimodal_pages_and_crop_transform(ctx):
    result = call(ctx, "pdf_render", {"pages": [1, 2], "zoom": 2})
    assert not result.is_error, result.text
    assert len(result.images) == 2
    assert "image" not in result.data["rendered"][0]
    for number, raw in enumerate(result.images):
        with Image.open(io.BytesIO(raw)) as image:
            assert list(image.size) == result.data["rendered"][number]["display_size"]
            assert image.getextrema()[0][0] < 100  # visible text, not just a blank page
    crop = call(ctx, "pdf_render", {"pages": [1], "crop": [40, 30, 180, 70], "zoom": 3}).data["rendered"][0]
    assert crop["display_size"] == [540, 210]
    assert crop["crop"] == [40, 30, 180, 70]
    assert crop["display_to_page"] == {"scale_x": 1 / 3, "scale_y": 1 / 3, "offset_x": 40, "offset_y": 30}


def test_scanned_image_pdf_detected_and_renders(ctx):
    image = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 180, 180), fill="red")
    image.save(ctx.ws.work / "scan.pdf", "PDF")
    result = call(ctx, "pdf_read", {"source": "scan.pdf"})
    assert not result.is_error, result.text
    page = result.data["page_details"][0]
    assert page["text_status"] == "likely_scanned" and page["has_images"] and not page["text"]
    view = call(ctx, "pdf_render", {"source": "scan.pdf", "crop": [40, 40, 100, 100]})
    assert not view.is_error, view.text
    with Image.open(io.BytesIO(view.images[0])) as shown:
        red, green, blue = shown.convert("RGB").getpixel((50, 50))
        assert red > 240 and green < 20 and blue < 20


def test_rotation_uses_displayed_page_coordinates(ctx):
    import pypdfium2 as pdfium

    image = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(image).rectangle((40, 40, 180, 180), fill="red")
    image.save(ctx.ws.work / "original.pdf", "PDF")
    with pdfium.PdfDocument(ctx.ws.work / "original.pdf") as document:
        with closing(document[0]) as page:
            page.set_rotation(90)
        document.save(ctx.ws.work / "rotated.pdf")
    info = call(ctx, "pdf_info", {"source": "rotated.pdf"}).data["page_details"][0]
    assert info["rotation"] == 90 and info["size"] == [300, 600]
    view = call(ctx, "pdf_render", {"source": "rotated.pdf", "crop": [130, 50, 100, 100]})
    assert not view.is_error, view.text
    with Image.open(io.BytesIO(view.images[0])) as shown:
        red, green, blue = shown.convert("RGB").getpixel((50, 50))
        assert red > 240 and green < 20 and blue < 20


def test_ocr_unavailable_is_explicit_without_a_download(ctx, monkeypatch):
    from vwb.index import vision

    monkeypatch.setattr(vision, "HAVE_VISION", False)
    _, options = pdf._args({"source": SOURCE, "ocr": "always"}, "read")
    with pytest.raises(ValueError, match="requires Apple Vision"):
        pdf._inspect((ctx.ws.work / SOURCE).read_bytes(), options)


def test_explicit_ocr_reads_real_scanned_text_where_available(ctx):
    from vwb.index import vision

    if not vision.HAVE_VISION:
        pytest.skip("Apple Vision unavailable")
    image = Image.new("RGB", (1000, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.text((50, 80), "Quarterly revenue 42 million", font=ImageFont.load_default(size=42), fill="black")
    image.save(ctx.ws.work / "scan-text.pdf", "PDF")
    result = call(ctx, "pdf_read", {"source": "scan-text.pdf", "ocr": "auto"})
    if result.is_error and "local OCR engine could not run" in result.text:
        pytest.skip("Apple Vision service unavailable in this execution sandbox")
    assert not result.is_error, result.text
    page = result.data["page_details"][0]
    assert "42 million" in page["text"]
    assert page["text_source"] == "ocr.apple-vision" and page["ocr_lines"]


@pytest.mark.parametrize("name,args", [
    ("pdf_info", {"start_page": 0}), ("pdf_info", {"start_page": True}),
    ("pdf_info", {"page_count": 21}), ("pdf_info", {"start_page": 4}),
    ("pdf_read", {"max_chars": 48001}), ("pdf_read", {"ocr": "auto", "page_count": 4}),
    ("pdf_read", {"ocr": True}), ("pdf_read", {"start_char": 99999}),
    ("pdf_render", {"pages": []}), ("pdf_render", {"pages": [True]}),
    ("pdf_render", {"pages": [4]}), ("pdf_render", {"pages": [1] * 5}),
    ("pdf_render", {"crop": [0, 0, 0, 1]}), ("pdf_render", {"crop": [0, 0, float("inf"), 1]}),
    ("pdf_render", {"crop": [400, 0, 100, 100]}), ("pdf_render", {"zoom": True}),
    ("pdf_render", {"max_edge": 1600}), ("pdf_search", {"query": ""}),
    ("pdf_search", {"query": "x", "match_case": "yes"}), ("pdf_search", {"query": "x", "max_results": 101}),
])
def test_invalid_requests_fail_through_registry(ctx, name, args):
    assert call(ctx, name, args).is_error


def test_private_traversal_symlink_and_fake_pdf_rejected(ctx):
    (ctx.ws.work / "link.pdf").symlink_to(ctx.ws.work / SOURCE)
    (ctx.ws.work / "link-folder").symlink_to(ctx.ws.work / "attachments", target_is_directory=True)
    (ctx.ws.work / "fake.pdf").write_text("not a pdf")
    (ctx.ws.work / "broken.pdf").write_bytes(b"%PDF-1.7 broken")
    for source in ("../outside.pdf", "/etc/passwd", "work/.private/a.pdf", "ledger/a.pdf", "link.pdf",
                   "link-folder/example/report.pdf", "fake.pdf", "broken.pdf"):
        assert call(ctx, "pdf_info", {"source": source}).is_error, source
    for source in ("work/" + SOURCE, "work:" + SOURCE):
        assert not call(ctx, "pdf_info", {"source": source}).is_error


def test_pinned_attachment_pdf_survives_working_copy_edits_and_deletion(ctx):
    from vwb.workspace import attachments

    ref, = attachments.ingest(ctx.ws, [ctx.ws.work / SOURCE])
    work = ctx.ws.work / ref["path"]
    work.write_bytes(b"agent changed its working copy")
    source = "attachment:" + ref["id"]
    first = call(ctx, "pdf_read", {"source": source})
    assert not first.is_error, first.text
    assert "Quarterly report" in first.data["page_details"][0]["text"]
    assert first.data["sha256"] == ref["sha256"] and first.data["source"] == source
    assert first.data["working_path"] == ref["path"]
    work.unlink()
    image = call(ctx, "pdf_render", {"source": source})
    assert not image.is_error and image.images
    assert call(ctx, "pdf_search", {"source": source, "query": "Revenue"}).data["matches"]
    assert call(ctx, "pdf_info", {"source": source}).data["pages"] == 3


def test_pinned_attachment_rejects_invalid_identity_symlink_and_changed_original(ctx):
    from vwb.workspace import attachments

    ref, = attachments.ingest(ctx.ws, [ctx.ws.work / SOURCE])
    original = attachments.original_path(ctx.ws, ref)
    source = "attachment:" + ref["id"]
    for invalid in ("attachment:../original", "attachment:", "attachment:" + "a" * 32):
        assert call(ctx, "pdf_info", {"source": invalid}).is_error
    original.unlink()
    original.symlink_to(ctx.ws.work / SOURCE)
    assert call(ctx, "pdf_info", {"source": source}).is_error
    original.unlink()
    original.write_bytes(b"%PDF-1.7 changed")
    assert call(ctx, "pdf_info", {"source": source}).is_error


def test_oversized_input_rejected_before_launch(ctx, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_BYTES", 5)
    monkeypatch.setattr(pdf.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not launch"))
    assert "64 MiB" in call(ctx, "pdf_info").text


def test_oversized_pinned_pdf_rejected_before_original_hash_or_launch(ctx, monkeypatch):
    from vwb.workspace import attachments

    ref, = attachments.ingest(ctx.ws, [ctx.ws.work / SOURCE])
    monkeypatch.setattr(pdf, "MAX_BYTES", ref["bytes"] - 1)
    monkeypatch.setattr(attachments, "original_path", lambda *args: pytest.fail("Do not hash an oversized original"))
    monkeypatch.setattr(pdf.subprocess, "run", lambda *args, **kwargs: pytest.fail("Do not launch a parser"))
    result = call(ctx, "pdf_info", {"source": "attachment:" + ref["id"]})
    assert result.is_error and "64 MiB" in result.text


def test_timeout_releases_shared_preview_slots_and_inherits_no_secrets(ctx, monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs["env"] == {"PYTHONIOENCODING": "utf-8"}
        assert kwargs["pass_fds"] and kwargs["cwd"] == "/"
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(pdf.subprocess, "run", timeout)
    for _ in range(3):
        assert "took too long" in call(ctx, "pdf_info").text
