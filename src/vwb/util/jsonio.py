"""JSON helpers: canonical form for hashing, atomic writes, JSONL appends."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


def _default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"Not JSON serialisable: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, indent=indent, ensure_ascii=False, default=_default)


def dumps_compact(obj: Any, width: int = 140, _indent: str = "") -> str:
    """JSON for model eyes: a container that fits in `width` characters stays on one line, larger ones
    break one child per line. Several times shorter than indent=1 for tables of small records, and
    still pageable by line."""
    flat = json.dumps(obj, ensure_ascii=False, default=_default, separators=(", ", ": "))
    if len(flat) + len(_indent) <= width or not isinstance(obj, (dict, list)) or not obj:
        return flat
    inner = _indent + " "
    if isinstance(obj, dict):
        rows = [f"{inner}{json.dumps(str(k), ensure_ascii=False)}: {dumps_compact(v, width, inner).lstrip()}"
                for k, v in obj.items()]
        return "{\n" + ",\n".join(rows) + f"\n{_indent}}}"
    rows = [inner + dumps_compact(v, width, inner).lstrip() for v in obj]
    return "[\n" + ",\n".join(rows) + f"\n{_indent}]"


def read_json(path: Path | str, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open() as fh:
        return json.load(fh)


def write_json(path: Path | str, obj: Any, indent: int | None = 2) -> Path:
    """Write atomically (temp file + rename) so readers never see half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(dumps(obj, indent=indent))
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def append_jsonl(path: Path | str, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, default=_default))
        fh.write("\n")


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
