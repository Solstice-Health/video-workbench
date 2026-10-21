"""PDFium examination in disposable processes with pinned, no-follow input fds.

No form environment, JavaScript, external-resource resolution or embedded-file
extraction is enabled. Like the browser preview, this is process isolation with
resource bounds, not an operating-system security sandbox for native parsers.
"""
from __future__ import annotations

import base64
import hashlib
import io
import itertools
import json
import math
import os
import stat
import subprocess
import sys
from contextlib import closing, contextmanager

from vwb.web.pdf_preview import _SLOTS, MAX_BYTES, MAX_PNG
from vwb.web.workspace_files import PUBLIC_ROOTS, open_file

MAX_PAGES = 10000
MAX_OUTPUT = MAX_PNG * 6
NOTE = ("Text is extracted in PDF content order, which may differ from reading order. Tables, columns, "
        "equations, fonts and images need visual inspection with pdf_render. No semantic table extraction is claimed.")


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number between {low} and {high}")
    return float(value)


def _args(args, operation):
    if operation not in {"info", "read", "search", "render"}:
        raise ValueError("Unknown PDF operation")
    source = args.get("source")
    if not isinstance(source, str) or not source or len(source) > 4096:
        raise ValueError("source must be a work-relative or public workspace PDF path")
    if source.startswith("attachment:"):
        path = source
    elif source.startswith("work:"):
        path = "work/" + source[5:]
    elif source.split("/", 1)[0] in PUBLIC_ROOTS:
        path = source
    else:
        path = "work/" + source
    options = {"operation": operation}
    if operation == "render":
        pages = args.get("pages", [1])
        if not isinstance(pages, list) or not 1 <= len(pages) <= 4:
            raise ValueError("pages must contain 1 to 4 page numbers")
        options["pages"] = [_int(page, "page", 1, MAX_PAGES) for page in pages]
        options["zoom"] = _number(args.get("zoom", 2), "zoom", .1, 8)
        options["max_edge"] = _int(args.get("max_edge", 1280), "max_edge", 64, 1568)
        if args.get("crop") is not None:
            crop = args["crop"]
            if not isinstance(crop, list) or len(crop) != 4:
                raise ValueError("crop must be [x, y, width, height] in displayed PDF page units")
            options["crop"] = [_number(value, "crop", 0, 1_000_000) for value in crop]
            if not all(value > 0 for value in options["crop"][2:]):
                raise ValueError("Crop width and height must be positive")
    else:
        options["start_page"] = _int(args.get("start_page", 1), "start_page", 1, MAX_PAGES)
        options["page_count"] = _int(args.get("page_count", 20 if operation == "search" else 5), "page_count", 1,
                                     50 if operation == "search" else 20)
        options["start_char"] = _int(args.get("start_char", 0), "start_char", 0, 100_000_000)
    if operation == "read":
        options["max_chars"] = _int(args.get("max_chars", 24000), "max_chars", 100, 48000)
        options["ocr"] = args.get("ocr", "off")
        if options["ocr"] not in {"off", "auto", "always"}:
            raise ValueError("ocr must be off, auto or always")
        # OCR defaults to one page rather than silently doing five raster/OCR passes.
        if options["ocr"] != "off":
            options["page_count"] = _int(args.get("page_count", 1), "page_count with OCR", 1, 3)
    if operation == "search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 256 or "\0" in query:
            raise ValueError("query must contain 1 to 256 characters without NUL")
        options["query"] = query
        options["max_results"] = _int(args.get("max_results", 30), "max_results", 1, 100)
        for key in ("match_case", "whole_word"):
            if type(args.get(key, False)) is not bool:
                raise ValueError(f"{key} must be a boolean")
            options[key] = args.get(key, False)
    return path, options


@contextmanager
def _source_file(ws, source):
    if not source.startswith("attachment:"):
        with open_file(ws, source) as handle:
            yield handle, None
        return
    from vwb.workspace import attachments

    row = attachments.canonical(ws, source.removeprefix("attachment:"))
    if type(row.get("bytes")) is not int or not 0 <= row["bytes"] <= MAX_BYTES:
        raise ValueError("PDF inspection is limited to 64 MiB per document")
    # Validate the immutable receipt, then pin every parent descriptor for the
    # parser. The child's content hash is checked again after examination so a
    # concurrent in-place edit cannot masquerade as the pinned chat original.
    attachments.original_path(ws, row)
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(ws.root, flags))
        for part in (".attachments", row["id"]):
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        fd = os.open("original", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1])
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Only regular PDF attachments can be inspected")
            yield handle, row
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def examine(ws, args: dict, operation: str) -> dict:
    path, options = _args(args, operation)
    with _source_file(ws, path) as (handle, attachment):
        if os.fstat(handle.fileno()).st_size > MAX_BYTES:
            raise ValueError("PDF inspection is limited to 64 MiB per document")
        if not handle.read(1024).lstrip().startswith(b"%PDF-"):
            raise ValueError("This file is not a PDF document")
        handle.seek(0)
        if not _SLOTS.acquire(timeout=1):
            raise ValueError("Two PDF operations are running. Try again in a moment.")
        try:
            result = subprocess.run([sys.executable, "-m", "vwb.documents.pdf", str(handle.fileno())],
                                    input=json.dumps(options).encode(), pass_fds=(handle.fileno(),),
                                    capture_output=True, timeout=60, env={"PYTHONIOENCODING": "utf-8"},
                                    cwd="/", check=False)
        except subprocess.TimeoutExpired:
            raise ValueError("PDF inspection took too long. Try fewer pages or a smaller region.") from None
        finally:
            _SLOTS.release()
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        raise ValueError("This PDF could not be examined. It may be damaged, encrypted or too complex.")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError("The PDF reader returned an invalid response") from None
    if value.get("error"):
        raise ValueError(value["error"])
    if attachment is not None:
        if value.get("sha256") != attachment["sha256"]:
            raise ValueError("The PDF attachment changed during inspection; the result was discarded")
        value.update(attachment_id=attachment["id"], working_path=attachment["path"])
    value["source"] = path
    return value


def _size(page):
    width, height = page.get_size()
    if not all(math.isfinite(value) and 0 < value <= 1_000_000 for value in (width, height)):
        raise ValueError("This PDF page has unsupported dimensions")
    return width, height


def _facts(page, number):
    import pypdfium2 as pdfium

    with closing(page.get_textpage()) as textpage:
        chars = textpage.count_chars()
    image_present = any(True for _ in itertools.islice(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=5), 1))
    return {"page": number, "size": list(_size(page)), "rotation": page.get_rotation(),
            "embedded_characters": chars, "has_images": image_present,
            "text_status": "embedded_text" if chars else "likely_scanned" if image_present else "no_extractable_text",
            "needs_visual_reading": chars == 0}


def _render(page, number, *, zoom=2, max_edge=1280, crop=None):
    width, height = _size(page)
    x, y, w, h = crop or [0, 0, width, height]
    if x >= width or y >= height:
        raise ValueError(f"Crop does not intersect page {number}")
    w, h = min(w, width - x), min(h, height - y)
    scale = min(zoom, max_edge / max(w, h))
    margins = (x, height - y - h, width - x - w, y)
    with closing(page.render(scale=scale, crop=margins, may_draw_forms=False, draw_annots=True,
                             limit_image_cache=True)) as bitmap:
        image = bitmap.to_pil()
        stream = io.BytesIO()
        image.save(stream, "PNG")
        data = stream.getvalue()
        if len(data) > MAX_PNG:
            raise ValueError("Rendered PDF page exceeds the image limit")
        # PDFium rounds the whole page and crop margins to integer pixels. This
        # transform accounts for those bounds instead of claiming requested crop
        # coordinates were exact at a fractional raster scale.
        full_w, full_h = math.ceil(width * scale), math.ceil(height * scale)
        sx, sy = width / full_w, height / full_h
        ox, oy = math.ceil(x * scale) * sx, math.ceil(y * scale) * sy
        info = {"page": number, "page_size": [width, height], "display_size": list(image.size),
                "requested_crop": crop, "crop": [ox, oy, image.width * sx, image.height * sy],
                "display_to_page": {"scale_x": sx, "scale_y": sy, "offset_x": ox, "offset_y": oy},
                "coordinate_space": "Displayed page, top-left origin; units usually 1/72 inch. Page UserUnit is not available.",
                "image": base64.b64encode(data).decode()}
        return info, image.convert("RGB").copy()


def _ocr(page, number):
    import numpy as np

    from vwb.index import vision

    if not vision.HAVE_VISION:
        raise ValueError("Local OCR requires Apple Vision on this host. Use pdf_render for visual reading; no OCR was run.")
    metadata, image = _render(page, number, zoom=3, max_edge=2400)
    try:
        result = vision.analyze(np.asarray(image), ocr=True, faces=False, humans=False, saliency=False)
    except RuntimeError:
        raise ValueError("The local OCR engine could not run on this host. Use pdf_render for visual reading; no OCR text was produced.") from None
    if len(result.lines) > 2000:
        raise ValueError("This page exceeds the OCR line limit. Use pdf_render on a smaller region.")
    lines = []
    transform = metadata["display_to_page"]
    for line in result.lines:
        x, y, w, h = line.box
        lines.append({"text": line.text, "confidence": line.conf,
                      "box": [x * transform["scale_x"], y * transform["scale_y"],
                              w * transform["scale_x"], h * transform["scale_y"]]})
    return "\n".join(line["text"] for line in lines), lines


def _inspect(data, options):
    import pypdfium2 as pdfium

    operation = options["operation"]
    with pdfium.PdfDocument(data) as document:
        count = len(document)
        if not 1 <= count <= MAX_PAGES:
            raise ValueError("PDF documents must have 1 to 10000 pages")
        result = {"pages": count, "sha256": hashlib.sha256(data).hexdigest(),
                  "untrusted_document_content": True, "layout_note": NOTE}
        if operation == "render":
            result["rendered"] = []
            for number in options["pages"]:
                if number > count:
                    raise ValueError(f"PDF page {number} does not exist (document has {count} pages)")
                with closing(document[number - 1]) as page:
                    image, _ = _render(page, number, zoom=options["zoom"], max_edge=options["max_edge"], crop=options.get("crop"))
                    image["image_index"] = len(result["rendered"])
                    result["rendered"].append(image)
            return result
        start = options["start_page"]
        if start > count:
            raise ValueError(f"PDF page {start} does not exist (document has {count} pages)")
        end = min(count, start + options["page_count"] - 1)
        result["range"] = [start, end]
        result["next_cursor"] = {"start_page": end + 1, "start_char": 0} if end < count else None
        result["page_details"] = []
        if operation == "info":
            result["metadata"] = {key: value[:4096] for key, value in document.get_metadata_dict(skip_empty=True).items()}
            bookmarks = []
            for item in itertools.islice(document.get_toc(max_depth=8), 501):
                dest = item.get_dest()
                page_index = dest.get_index() if dest else None
                bookmarks.append({"title": item.get_title()[:1000], "level": item.level,
                                  "page": page_index + 1 if page_index is not None and 0 <= page_index < count else None})
            result["outline"], result["outline_truncated"] = bookmarks[:500], len(bookmarks) > 500
            result["form_type"] = document.get_formtype()
            result["forms_note"] = "Form environments, PDF scripts and embedded files are not opened. Inspect rendered appearances; this does not validate canonical form values."
        remaining = options.get("max_chars", 0)
        matches = []
        for number in range(start, end + 1):
            with closing(document[number - 1]) as page:
                facts = _facts(page, number)
                facts["label"] = document.get_page_label(number - 1)
                result["page_details"].append(facts)
                if operation == "info":
                    continue
                offset = options["start_char"] if number == start else 0
                if operation == "read":
                    use_ocr = options["ocr"] == "always" or (options["ocr"] == "auto" and not facts["embedded_characters"])
                    if use_ocr:
                        full, lines = _ocr(page, number)
                        chars = len(full)
                        text = full[offset:offset + remaining]
                        consumed = min(remaining, max(0, chars - offset))
                        facts["text_source"] = "ocr.apple-vision"
                        facts["ocr_note"] = "Approximate OCR, English recognition configuration; verify names, numbers and tables against the page image."
                        # Return only lines covered by this slice; do not duplicate
                        # the entire OCR payload on every continuation.
                        line_offset, selected = 0, []
                        for line in lines:
                            if line_offset < offset + consumed and line_offset + len(line["text"]) > offset:
                                selected.append({key: value for key, value in line.items() if key != "text"} |
                                                {"start_char": line_offset, "end_char": line_offset + len(line["text"])})
                            line_offset += len(line["text"]) + 1
                        facts["ocr_lines"] = selected
                    else:
                        chars = facts["embedded_characters"]
                        consumed = min(remaining, max(0, chars - offset))
                        with closing(page.get_textpage()) as textpage:
                            text = textpage.get_text_range(offset, consumed) if consumed else ""
                        facts["text_source"] = "embedded"
                    if offset > chars:
                        raise ValueError(f"start_char exceeds the text on page {number}")
                    facts.update(text=text, start_char=offset, end_char=offset + consumed, text_characters=chars,
                                 truncated=offset + consumed < chars)
                    remaining -= max(len(text), consumed)
                    if facts["truncated"] or (remaining <= 0 and number < count):
                        result["next_cursor"] = {"start_page": number if facts["truncated"] else number + 1,
                                                 "start_char": offset + consumed if facts["truncated"] else 0}
                        result["range"][1] = number
                        break
                else:
                    with closing(page.get_textpage()) as textpage:
                        if offset > facts["embedded_characters"]:
                            raise ValueError(f"start_char exceeds the text on page {number}")
                        with closing(textpage.search(options["query"], index=offset, match_case=options["match_case"],
                                                     match_whole_word=options["whole_word"])) as search:
                            while (hit := search.get_next()) is not None:
                                index, length = hit
                                if len(matches) >= options["max_results"]:
                                    result["next_cursor"] = {"start_page": number, "start_char": index}
                                    result["range"][1] = number
                                    result["matches"] = matches
                                    result["matches_limited"] = True
                                    return result
                                snippet_start = max(0, index - 100)
                                snippet_count = min(facts["embedded_characters"] - snippet_start, length + 200)
                                matches.append({"page": number, "start_char": index, "length": length,
                                                "snippet": textpage.get_text_range(snippet_start, snippet_count)})
        if operation == "search":
            result["matches"], result["matches_limited"] = matches, False
        return result


def _main():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    options = json.loads(sys.stdin.buffer.read(16384))
    with os.fdopen(int(sys.argv[1]), "rb") as stream:
        stream.seek(0)
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Oversized PDF")
    try:
        value = _inspect(data, options)
    except ValueError as exc:
        value = {"error": str(exc)}
    sys.stdout.write(json.dumps(value))


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        # Native parser diagnostics can contain paths/document data; keep private.
        sys.exit(1)
