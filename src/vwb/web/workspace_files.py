"""Lazy, bounded public request files; no private request state or host paths.

The HTTP layer supplies a registered Workspace and adds URLs. Serve preview bytes
from ``open_file`` itself, never by reopening a previously validated pathname.
HTML/SVG/PDF response isolation and CSRF/origin checks belong to that HTTP layer.
This is a filename/path policy, not a content secret scanner: deliberately placing
credentials in an otherwise public artifact cannot be detected here.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import mimetypes
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from vwb.workspace.request import Workspace

PUBLIC_ROOTS = frozenset({
    "sources", "media", "assets", "work", "canvas", "renders", "review", "index", "copy",
    "qc", "release", "plates",
})
MAX_DIRECTORY_ENTRIES = 10_000
MAX_PAGE_ENTRIES = 200
MAX_TEXT_BYTES = 128 * 1024
SNIFF_BYTES = 8192
MAX_PATH_BYTES = 4096
MAX_PATH_DEPTH = 32

_PRIVATE_NAMES = frozenset({
    "agent", "memory", "ledger", "swarm", "asks", "node_modules", "__pycache__",
    "credentials", "secrets", "sessions", "cookies",
})
_SECRET_STEMS = frozenset({
    "credential", "credentials", "secret", "secrets", "access_token", "refresh_token", "auth_token",
    "api_key", "apikey", "token", "session", "sessions", "cookie", "cookies", "auth", "oauth",
    "service-account", "service_account", "client_secret", "lead_session", "mcp_servers", "mcp-servers",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
})
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore", ".kdbx"})
_TEXT = frozenset({".txt", ".csv", ".tsv", ".srt", ".vtt", ".log", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"})
_CODE = frozenset({".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sass", ".less", ".sh", ".bash", ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".swift", ".rb"})
_IMAGES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif", ".svg"})
_VIDEO = frozenset({".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"})
_AUDIO = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac"})
_TEXT_KINDS = frozenset({"text", "code", "json", "markdown"})
_MIME = {".md": "text/markdown", ".markdown": "text/markdown", ".ts": "text/plain", ".tsx": "text/plain",
         ".jsx": "text/plain", ".py": "text/plain", ".yaml": "text/plain", ".yml": "text/plain",
         ".toml": "text/plain", ".mkv": "video/x-matroska", ".m4v": "video/mp4", ".m4a": "audio/mp4"}


def _public_name(name: str) -> bool:
    lowered = name.casefold()
    if not name or name.startswith(".") or lowered in _PRIVATE_NAMES:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in name) or "\\" in name:
        return False
    if Path(lowered).suffix in _SECRET_SUFFIXES:
        return False
    return not any(lowered == stem or any(lowered.startswith(stem + sep) for sep in (".", "-", "_"))
                   for stem in _SECRET_STEMS)


def _parts(path: str, *, root_ok: bool = False) -> tuple[str, ...]:
    if not isinstance(path, str) or len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError("The workspace path is invalid or too long.")
    if not path and root_ok:
        return ()
    parts = tuple(path.split("/"))
    if len(parts) > MAX_PATH_DEPTH or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Use a relative workspace path without traversal.")
    if parts[0] not in PUBLIC_ROOTS or any(not _public_name(part) for part in parts):
        raise PermissionError("This workspace path is private or unavailable.")
    return parts


def _translate_error(exc: OSError) -> None:
    if isinstance(exc, PermissionError) or exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM):
        raise PermissionError("This workspace path is not an accessible regular file or directory.") from None
    if exc.errno == errno.ENOENT:
        raise FileNotFoundError("The workspace file or directory was not found.") from None
    raise OSError("The workspace file could not be opened.") from None


@contextmanager
def _open(ws: Workspace, parts: tuple[str, ...], *, directory: bool) -> Iterator[int]:
    """Walk pinned directory descriptors; a rename cannot redirect later opens."""
    descriptors: list[int] = []
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        descriptors.append(os.open(ws.root, flags))
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            child_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last or directory:
                child_flags |= os.O_DIRECTORY
            descriptors.append(os.open(part, child_flags, dir_fd=descriptors[-1]))
        mode = os.fstat(descriptors[-1]).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise PermissionError("Only regular workspace files and directories are available.")
        yield descriptors[-1]
    except OSError as exc:
        # Do not expose absolute host paths in API error strings.
        _translate_error(exc)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def open_file(ws: Workspace, path: str) -> Iterator[BinaryIO]:
    """Open an allowed regular file securely; caller owns no pathname reopening.

    The binary stream is seekable for range responses. It closes on leaving this
    context; it remains attached to the original inode if a filename is replaced.
    """
    parts = _parts(path)
    if len(parts) < 2:
        raise PermissionError("Files must be inside a public workspace directory.")
    with _open(ws, parts, directory=False) as descriptor:
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            yield stream


def _declared_type(name: str) -> tuple[str, str]:
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGES:
        kind = "image"
    elif suffix in _VIDEO:
        kind = "video"
    elif suffix in _AUDIO:
        kind = "audio"
    elif suffix in {".html", ".htm"}:
        kind = "html"
    elif suffix == ".pdf":
        kind = "pdf"
    elif suffix == ".json":
        kind = "json"
    elif suffix in {".md", ".markdown"}:
        kind = "markdown"
    elif suffix in _CODE:
        kind = "code"
    elif suffix in _TEXT or Path(name).name in {"HEAD", "Makefile", "Dockerfile", "LICENSE", "README"}:
        kind = "text"
    else:
        kind = "download"
    mime = _MIME.get(suffix) or mimetypes.guess_type(name)[0]
    if kind in _TEXT_KINDS and not mime:
        mime = "text/plain"
    return kind, mime or "application/octet-stream"


def _utf8_prefix(data: bytes) -> str | None:
    # Up to three trailing bytes may be an incomplete code point at a read bound.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason != "unexpected end of data" or exc.end != len(data):
            return None
        text = data[:exc.start].decode("utf-8")
    if any(ord(char) < 32 and char not in "\n\r\t\f" for char in text):
        return None
    return text


def _verified_type(name: str, sample: bytes) -> tuple[str, str, str | None]:
    kind, mime = _declared_type(name)
    suffix = Path(name).suffix.lower()
    valid = True
    if kind in _TEXT_KINDS or kind == "html":
        valid = _utf8_prefix(sample) is not None
    elif kind == "pdf":
        valid = sample.startswith(b"%PDF-")
    elif kind == "image":
        checks = {
            ".png": sample.startswith(b"\x89PNG\r\n\x1a\n"),
            ".jpg": sample.startswith(b"\xff\xd8\xff"), ".jpeg": sample.startswith(b"\xff\xd8\xff"),
            ".gif": sample.startswith((b"GIF87a", b"GIF89a")),
            ".webp": sample.startswith(b"RIFF") and sample[8:12] == b"WEBP",
            ".bmp": sample.startswith(b"BM"),
            ".tif": sample.startswith((b"II*\x00", b"MM\x00*")),
            ".tiff": sample.startswith((b"II*\x00", b"MM\x00*")),
            ".svg": _utf8_prefix(sample) is not None and b"<svg" in sample.lower(),
        }
        valid = checks.get(suffix, sample[4:8] == b"ftyp" and sample[8:12] in {b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"})
    elif kind == "video":
        valid = (sample[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free"} if suffix in {".mp4", ".mov", ".m4v"}
                 else sample.startswith(b"\x1aE\xdf\xa3") if suffix in {".mkv", ".webm"}
                 else sample.startswith(b"RIFF") and sample[8:12] == b"AVI ")
    elif kind == "audio":
        valid = (sample.startswith(b"RIFF") and sample[8:12] == b"WAVE" if suffix == ".wav"
                 else sample.startswith(b"fLaC") if suffix == ".flac"
                 else sample.startswith(b"OggS") if suffix in {".ogg", ".oga"}
                 else sample[4:8] == b"ftyp" if suffix == ".m4a"
                 else sample.startswith(b"ID3") or (len(sample) >= 2 and sample[0] == 255 and sample[1] & 224 == 224))
    if not valid:
        return "download", "application/octet-stream", "The file prefix does not match its declared preview type."
    return kind, mime, None


def _stamp(info: os.stat_result) -> str:
    value = f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _entry(path: str, info: os.stat_result) -> dict:
    directory = stat.S_ISDIR(info.st_mode)
    kind, mime = _declared_type(path)
    return {"name": path.rsplit("/", 1)[-1], "path": path, "kind": "directory" if directory else "file",
            "is_file": not directory, "mime": None if directory else mime, "size": None if directory else info.st_size,
            "preview_kind": None if directory else kind, "type_verified": False,
            "modified": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(), "etag": _stamp(info)}


def _cursor(value: str | None, stamp: str) -> int:
    if value is None:
        return 0
    try:
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError
        parsed = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        offset = parsed["offset"]
        if parsed["stamp"] != stamp:
            raise ValueError("The directory changed. Refresh its file list.")
        if type(offset) is not int or not 0 <= offset <= MAX_DIRECTORY_ENTRIES:
            raise ValueError
        return offset
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("The directory cursor is invalid. Refresh its file list.") from exc


def listing(ws: Workspace, path: str = "", *, cursor: str | None = None, limit: int = MAX_PAGE_ENTRIES) -> dict:
    """One directory, max 200 visible entries/page and 10k names scanned.

    The scan cap is explicit; it never claims exhaustive coverage of a larger
    directory. Hidden/private entries are deliberately absent. Cursors expire on
    directory mutation; a list refresh is required rather than silently skipping.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ENTRIES:
        raise ValueError(f"The directory page limit must be between 1 and {MAX_PAGE_ENTRIES}.")
    parts = _parts(path, root_ok=True)
    with _open(ws, parts, directory=True) as descriptor:
        stamp = _stamp(os.fstat(descriptor))
        offset = _cursor(cursor, stamp)
        names: list[tuple[bool, str]] = []
        capped = False
        with os.scandir(descriptor) as entries:
            for scanned, entry in enumerate(entries):
                if scanned >= MAX_DIRECTORY_ENTRIES:
                    capped = True
                    break
                if not _public_name(entry.name) or (not parts and entry.name not in PUBLIC_ROOTS):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    directory = entry.is_dir(follow_symlinks=False)
                    if not parts and not directory:
                        continue
                    if directory or entry.is_file(follow_symlinks=False):
                        names.append((directory, entry.name))
                except FileNotFoundError:
                    continue
        names.sort(key=lambda item: (not item[0], item[1].casefold(), item[1]))
        result = []
        for directory, name in names[offset:offset + limit]:
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                continue
            result.append(_entry("/".join((*parts, name)), info))
        if _stamp(os.fstat(descriptor)) != stamp:
            raise ValueError("The directory changed. Refresh its file list.")
    next_offset = offset + limit
    next_cursor = (base64.urlsafe_b64encode(json.dumps({"stamp": stamp, "offset": next_offset}).encode()).decode().rstrip("=")
                   if next_offset < len(names) else None)
    return {"path": path, "entries": result, "next_cursor": next_cursor, "truncated": capped,
            "scan_limit": MAX_DIRECTORY_ENTRIES,
            "notice": "This directory exceeds the bounded scan; only the first scanned entries are shown." if capped else None}


def metadata(ws: Workspace, path: str) -> dict:
    """Metadata and bounded UTF-8 text; no decoding media or hashing whole files.

    ``truncated`` describes the text preview only. Download/source URLs can still
    serve the complete pinned file. Header checks are not full media validation.
    """
    with open_file(ws, path) as stream:
        before = os.fstat(stream.fileno())
        result = _entry(path, before)
        sample = stream.read(SNIFF_BYTES)
        kind, mime, warning = _verified_type(path, sample)
        result.update(preview_kind=kind, mime=mime, type_verified=True, type_warning=warning, truncated=False)
        if kind in _TEXT_KINDS:
            stream.seek(0)
            data = stream.read(MAX_TEXT_BYTES)
            text = _utf8_prefix(data)
            if text is None:
                result.update(preview_kind="download", mime="application/octet-stream",
                              type_warning="The bounded preview is not UTF-8 text.")
            else:
                used = len(text.encode("utf-8"))
                result.update(text=text, text_bytes=used, text_limit_bytes=MAX_TEXT_BYTES, encoding="utf-8",
                              truncated=used < before.st_size)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("The file changed while its preview was read. Open it again.")
        return result
