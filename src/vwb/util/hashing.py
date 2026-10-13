"""Content hashing. Everything the harness caches is keyed by content, never by name."""

from __future__ import annotations

import hashlib
import secrets as _secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from vwb.util.jsonio import canonical_json


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


@lru_cache(maxsize=4096)
def _sha256_file_cached(path: str, size: int, mtime_ns: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path | str) -> str:
    st = Path(path).stat()
    return _sha256_file_cached(str(Path(path).resolve()), st.st_size, st.st_mtime_ns)


def short(digest: str, n: int = 12) -> str:
    return digest[:n]


def new_id(prefix: str, nbytes: int = 4) -> str:
    return f"{prefix}-{_secrets.token_hex(nbytes)}"
