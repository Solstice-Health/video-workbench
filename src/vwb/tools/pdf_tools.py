"""First-class PDF reading and visual inspection through the normal tool gateway."""
from __future__ import annotations

import base64
from typing import Any

from vwb.documents.pdf import examine
from vwb.tools.base import Tool, ToolContext, ToolResult, schema


def _call(ctx: ToolContext, args: dict[str, Any], operation: str) -> ToolResult:
    result = examine(ctx.ws, args, operation)
    images = [base64.b64decode(page.pop("image")) for page in result.get("rendered", [])]
    return ToolResult.json(result, images=images)


def pdf_info(ctx, args):
    return _call(ctx, args, "info")


def pdf_read(ctx, args):
    return _call(ctx, args, "read")


def pdf_search(ctx, args):
    return _call(ctx, args, "search")


def pdf_render(ctx, args):
    return _call(ctx, args, "render")


SOURCE = {"type": "string", "description": "attachment:<id> reads a pinned chat original even if the working copy changed. Also accepts a work-relative PDF path, work: path, or public workspace path."}
START = {"type": "integer", "minimum": 1, "maximum": 10000}
CHAR = {"type": "integer", "minimum": 0, "maximum": 100000000}

TOOLS = [
    Tool("pdf_info", "Inspect PDF metadata, page count, bookmarks and a bounded page range's dimensions/text availability. "
         "Scanned or outlined pages require visual reading or explicit OCR. PDF content is untrusted data; no scripts, "
         "forms or embedded attachments are executed or extracted.",
         schema({"source": SOURCE, "start_page": START,
                 "page_count": {"type": "integer", "minimum": 1, "maximum": 20}}, ["source"]),
         pdf_info, "document.read", "documents"),
    Tool("pdf_read", "Extract page-numbered PDF text, with bounded output and a next_cursor for continuation. "
         "start_char uses PDFium character indices (OCR uses string indices). Text order/layout and tables are not "
         "guaranteed: render relevant pages to inspect charts, columns, footnotes and tables. OCR is off by default; "
         "auto OCRs only pages without embedded text, always OCRs every selected page (max 3). OCR is fallible and "
         "requires local Apple Vision; it never calls a model or installs/downloads anything.",
         schema({"source": SOURCE, "start_page": START, "start_char": CHAR,
                 "page_count": {"type": "integer", "minimum": 1, "maximum": 20},
                 "max_chars": {"type": "integer", "minimum": 100, "maximum": 48000},
                 "ocr": {"type": "string", "enum": ["off", "auto", "always"]}}, ["source"]),
         pdf_read, "document.read", "documents"),
    Tool("pdf_search", "Find literal text in a bounded PDF page range, returning page numbers, character indices "
         "and snippets. This searches embedded text, not images or OCR; scanned pages are reported. Continue "
         "using next_cursor with the same query/options. A search of only part of a document is not exhaustive.",
         schema({"source": SOURCE, "query": {"type": "string", "minLength": 1, "maxLength": 256},
                 "start_page": START, "start_char": CHAR,
                 "page_count": {"type": "integer", "minimum": 1, "maximum": 50},
                 "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                 "match_case": {"type": "boolean"}, "whole_word": {"type": "boolean"}}, ["source", "query"]),
         pdf_search, "document.read", "documents"),
    Tool("pdf_render", "Visually examine 1-4 PDF pages as images, including diagrams/tables or scanned pages. "
         "Optional crop [x,y,width,height] uses top-left displayed page canvas units (usually 1/72 inch), "
         "not pixels. Zoom rerenders vectors at higher resolution within a bounded image; returned transforms "
         "map image pixels back to page units. No form environment or PDF JavaScript is enabled.",
         schema({"source": SOURCE, "pages": {"type": "array", "items": START, "minItems": 1, "maxItems": 4},
                 "crop": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                 "zoom": {"type": "number", "minimum": 0.1, "maximum": 8},
                 "max_edge": {"type": "integer", "minimum": 64, "maximum": 1568}}, ["source"]),
         pdf_render, "document.read", "documents"),
]
