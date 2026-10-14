"""Bounded, process-isolated PDF pages for browsers without a native reader.

The renderer receives an already validated file descriptor, never a pathname.
It runs no PDF JavaScript or form environment. A timeout and concurrency limit
keep a malformed document from blocking the Workbench HTTP process.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import subprocess
import sys
import threading

from vwb.web.workspace_files import open_file

MAX_BYTES = 64 * 1024 * 1024
MAX_EDGE = 1600
MAX_PNG = 10 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)


def render(ws, path: str, page: int = 1) -> dict:
    if type(page) is not int or not 1 <= page <= 10_000:
        raise ValueError("Choose a PDF page between 1 and 10000.")
    with open_file(ws, path) as handle:
        if os.fstat(handle.fileno()).st_size > MAX_BYTES:
            raise ValueError("PDF previews are limited to 64 MiB. Open the original document instead.")
        if not handle.read(1024).lstrip().startswith(b"%PDF-"):
            raise ValueError("This file is not a PDF document.")
        handle.seek(0)
        if not _SLOTS.acquire(timeout=1):
            raise ValueError("Two PDF pages are rendering. Try again in a moment.")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "vwb.web.pdf_preview", str(handle.fileno()), str(page)],
                pass_fds=(handle.fileno(),), capture_output=True, timeout=20,
                env={"PYTHONIOENCODING": "utf-8"}, cwd="/", check=False,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("This PDF page took too long to render. Open the original document instead.") from None
        finally:
            _SLOTS.release()
    if result.returncode or len(result.stdout) > MAX_PNG * 2:
        raise ValueError("This PDF could not be previewed. It may be damaged, encrypted or too complex.")
    value = json.loads(result.stdout)
    if value.get("error"):
        raise ValueError(value["error"])
    return value


def _page(data: bytes, page: int) -> dict:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(data) as document:
        count = len(document)
        if not 1 <= count <= 10_000 or not 1 <= page <= count:
            return {"error": "That PDF page does not exist, or the document exceeds 10000 pages."}
        sheet = document[page - 1]
        try:
            width, height = sheet.get_size()
            if not all(math.isfinite(size) and 0 < size <= 1_000_000 for size in (width, height)):
                return {"error": "This PDF page has unsupported dimensions."}
            scale = min(2.0, MAX_EDGE / max(width, height))
            bitmap = sheet.render(scale=scale)
            try:
                image = bitmap.to_pil()
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                encoded = stream.getvalue()
                if len(encoded) > MAX_PNG:
                    return {"error": "This PDF page is too large to preview. Open the original document instead."}
                return {"page": page, "pages": count, "width": image.width, "height": image.height,
                        "image": "data:image/png;base64," + base64.b64encode(encoded).decode()}
            finally:
                bitmap.close()
        finally:
            sheet.close()


def _main() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    with os.fdopen(int(sys.argv[1]), "rb") as handle:
        handle.seek(0)  # The parent's buffered signature read may have advanced the descriptor.
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Oversized PDF")
    sys.stdout.write(json.dumps(_page(data, int(sys.argv[2]))))


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        # Do not return native parser diagnostics, paths or document contents.
        sys.exit(1)
