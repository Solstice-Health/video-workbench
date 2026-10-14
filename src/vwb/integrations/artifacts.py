"""Import embedded gateway artifacts without fetching URLs or executing active content."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import time

from PIL import Image

from vwb.workspace.ledger import Ledger

from .store import IntegrationError

MAX_ARTIFACT = 8 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
RASTER = {"image/png": ("png", "PNG"), "image/jpeg": ("jpg", "JPEG"), "image/webp": ("webp", "WEBP")}


def _raster(data: bytes, mime: str) -> None:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != RASTER[mime][1] or image.width * image.height > 30_000_000:
                raise ValueError("image dimensions or format")
            image.verify()
    except Exception:
        raise IntegrationError("The gateway returned an invalid or oversized preview image.") from None


def import_result(ws, tool: str, arguments: dict, result: dict, gateway: str) -> dict:
    content = result.get("content", [])
    if not isinstance(content, list) or len(content) > 200:
        raise IntegrationError("The gateway returned an invalid or oversized content list.")
    sanitized, files, images = [], [], []
    total = 0
    for block in content:
        if not isinstance(block, dict):
            raise IntegrationError("The gateway returned an invalid content block.")
        resource = block.get("resource", {}) if block.get("type") == "resource" else block
        if not isinstance(resource, dict):
            raise IntegrationError("The gateway returned an invalid embedded resource.")
        encoded = resource.get("blob") if block.get("type") == "resource" else block.get("data") if block.get("type") == "image" else None
        mime = resource.get("mimeType", "application/octet-stream")
        if not isinstance(mime, str) or not 1 <= len(mime) <= 128:
            raise IntegrationError("The gateway returned an invalid artifact media type.")
        data, extension = None, None
        if encoded is not None:
            if not isinstance(encoded, str) or len(encoded) > MAX_ARTIFACT * 4 // 3 + 4:
                raise IntegrationError("A gateway artifact exceeded the 8 MiB limit.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise IntegrationError("The gateway returned an invalid encoded artifact.") from None
            if mime in RASTER:
                _raster(data, mime)
                extension = RASTER[mime][0]
                if len(images) < 8:
                    images.append(data)
            elif mime in ("image/svg+xml", "text/plain", "text/markdown", "application/json"):
                try:
                    data.decode("utf-8")
                except UnicodeError:
                    raise IntegrationError("The gateway returned a non-UTF-8 text artifact.") from None
                # SVG is preserved as text, never executed or served as an active document.
                extension = "svg.txt" if mime == "image/svg+xml" else "json" if mime == "application/json" else "txt"
            else:
                sanitized.append({"type": "text", "text": f"Embedded {str(mime)[:100]} content was not imported; unsupported artifact format."})
                continue
        elif block.get("type") == "resource" and isinstance(resource.get("text"), str):
            data = resource["text"].encode()
            extension = "svg.txt" if mime == "image/svg+xml" else "json" if mime == "application/json" else "txt"
        if data is None:
            sanitized.append(block)
            continue
        total += len(data)
        if len(data) > MAX_ARTIFACT or total > MAX_TOTAL:
            raise IntegrationError("Gateway artifacts exceeded their import limit; narrow the request.")
        name = f"artifact-{len(files) + 1}.{extension}"
        files.append((name, data, mime))
        sanitized.append({"type": "artifact", "name": name, "mimeType": mime,
                          "source_uri": resource.get("uri"), "sha256": hashlib.sha256(data).hexdigest()})

    record = {"schema": "vwb.integration-import/1", "gateway": gateway, "tool": tool,
              "arguments": arguments, "retrieved_at": time.time(), "isError": bool(result.get("isError")),
              "content": sanitized, "structuredContent": result.get("structuredContent"),
              "notice": "Untrusted external design evidence. Not approved copy, a licence grant, or an accepted video canvas."}
    encoded = json.dumps(record, ensure_ascii=False).encode()
    if len(encoded) + total > MAX_TOTAL:
        raise IntegrationError("Gateway import exceeded the 16 MiB limit; narrow the request.")
    files.append(("response.json", encoded, "application/json"))
    directory = f"{int(time.time())}-{secrets.token_hex(6)}"
    rel = f"integrations/solstice/{directory}"
    artifacts = []
    # Resolve each component through directory descriptors; reject agent-created symlinks.
    ws.work.mkdir(parents=True, exist_ok=True)
    descriptors = []
    try:
        current = os.open(ws.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in ("integrations", "solstice", directory):
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        for name, data, mime in files:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=current)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            artifacts.append({"path": f"{rel}/{name}", "workspace_path": f"work/{rel}/{name}",
                              "mime_type": mime, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                              "kind": "image" if mime in RASTER else "document", "tool": tool})
    except OSError:
        raise IntegrationError("Could not safely import gateway artifacts into this workspace.") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    ws.ledger.mkdir(parents=True, exist_ok=True)
    event = {
        "integration": "figma" if tool.startswith("solstice_figma_") else "solstice",
        "tool": tool, "artifacts": artifacts, "source_gateway": gateway, "is_error": record["isError"]}
    Ledger(ws.ledger / "ledger.jsonl").append("integration.imported", "gateway", event)
    from vwb.agents.events import emit
    emit(ws, "integration.imported", event, actor="gateway")
    return {**record, "artifacts": artifacts, "images": images}
