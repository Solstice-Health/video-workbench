"""Bounded original raster pixels shared by inspection and image editing.

Reading an image does not register a video or imply an approved/master asset.
Attachment IDs select immutable originals; work paths select current bytes.
"""
from __future__ import annotations

import hashlib
import io
import os
import stat
import warnings
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageOps

from vwb.web.workspace_files import open_file
from vwb.workspace.attachments import canonical
from vwb.workspace.registry import Registry

MAX_SOURCE_BYTES = 50 * 1024**2
MAX_SOURCE_PIXELS = 40_000_000


@contextmanager
def _original(ws, attachment_id):
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors.append(os.open(ws.root, flags))
        for part in (".attachments", attachment_id):
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        descriptors.append(os.open("original", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                   dir_fd=descriptors[-1]))
        if not stat.S_ISREG(os.fstat(descriptors[-1]).st_mode):
            raise ValueError("The attachment original is not a regular file.")
        with os.fdopen(os.dup(descriptors[-1]), "rb") as stream:
            yield stream
    except OSError:
        raise ValueError("The attachment original is unavailable or unsafe to read.") from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_image(ws, reference: str) -> tuple[Image.Image, str, dict]:
    """Return full-resolution, EXIF-oriented RGBA pixels and byte-level lineage."""
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 4096:
        raise ValueError("Use attachment:<id>, a work-relative image path, or slot:<name>@<revision>.")
    reference = reference.strip()
    expected_hash = None
    if reference.startswith("attachment:"):
        row = canonical(ws, reference[11:])
        expected_hash = row["sha256"]
        reference = f"attachment:{row['id']}"
        lineage = {"kind": "attachment", "id": row["id"], "ref": reference}
        opened = _original(ws, row["id"])
    elif reference.startswith("slot:"):
        row = Registry(ws.assets).get(reference)
        relative = Path(row["path"]).relative_to(ws.assets).as_posix()
        expected_hash = row["sha256"]
        reference = f"slot:{row['slot']}@{row['rev']}"
        lineage = {"kind": "asset", "ref": reference, "prov": row["prov"]}
        opened = open_file(ws, "assets/" + relative)
    else:
        reference = reference.removeprefix("work:").removeprefix("work/")
        if Path(reference).is_absolute():
            raise ValueError("Use an image path relative to the work folder.")
        lineage = {"kind": "work_file", "path": reference}
        opened = open_file(ws, "work/" + reference)
    with opened as stream:
        before = os.fstat(stream.fileno())
        if not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise ValueError("Image input must be between 1 byte and 50 MiB.")
        data = stream.read(MAX_SOURCE_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Image changed during reading; finish writing it and retry.")
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Image input exceeds 50 MiB.")
    digest = hashlib.sha256(data).hexdigest()
    if expected_hash and digest != expected_hash:
        raise ValueError("Image integrity check failed: the original bytes changed after the receipt was saved.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"}:
                    raise ValueError("Unsupported raster image; export PNG, JPEG or WebP first.")
                if image.width * image.height > MAX_SOURCE_PIXELS:
                    raise ValueError("Image input exceeds 40 megapixels.")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Choose a single still image; export the desired animation frame first.")
                lineage["has_alpha"] = "A" in image.getbands() or "transparency" in image.info
                pixels = ImageOps.exif_transpose(image).convert("RGBA")
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Cannot decode this raster image safely; export PNG, JPEG or WebP first.") from None
    lineage.update(sha256=digest, coordinate_space="EXIF-oriented source pixels")
    return pixels, reference, lineage
