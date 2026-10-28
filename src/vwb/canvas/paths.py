"""Bounded output paths and descriptor-pinned reads of canvas/render records."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


def output_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) or ".." in value:
        raise ValueError("output must be a simple identifier of at most 128 characters")
    return value


def version_number(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 999_999_999:
        raise ValueError("version must be a positive integer below one billion")
    return value


def checked_path(root: Path, *parts: str) -> Path:
    """Reject existing symlinks; readers additionally pin each directory below."""
    path = root
    for part in ("", *parts):
        if part:
            if part in (".", "..") or "/" in part or "\\" in part:
                raise ValueError("invalid output path component")
            path /= part
        if path.is_symlink():
            raise PermissionError("Output records cannot use symbolic links")
    return path


def read_text(root: Path, *parts: str) -> str:
    checked_path(root, *parts)
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        if not stat.S_ISREG(os.fstat(descriptors[-1]).st_mode):
            raise PermissionError("Output records must be regular files")
        with os.fdopen(os.dup(descriptors[-1]), "r") as stream:
            return stream.read()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_record(root: Path, *parts: str, default: Any = None) -> Any:
    try:
        return json.loads(read_text(root, *parts))
    except FileNotFoundError:
        return default
