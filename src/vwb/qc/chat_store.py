"""Private, version-pinned QC findings and explicit editor handoffs.

``QCStore(ws)`` takes the *parent request* Workspace, never the QC runtime's
workspace. All records live in ``ws.root/.qc-chat/`` outside agent work folders.
``events.jsonl`` is the authoritative, locked append-only journal; ``items.json``
is an atomic, recoverable projection. No automatic action records a pass verdict.

Public methods return detached JSON dictionaries:
* targets(*, limit=1000): newest rendered versions first, with stat-cached hashes.
* verify_target(target): the same target descriptor, with uncached byte checks.
* create(payload, actor='human', *, model=None): strict fields; category defaults
  to visual, priority to warning, lane to shared. Frame ranges are half-open.
  Optional pin={sha256, canvas_file_sha256} fences changes since inspection.
* list_items(audience='qc') / get(id, audience='qc'): editor sees only the latest
  successfully sent frozen snapshot of each finding. All batches remain journaled.
* transition(id, status, actor='human', *, expected_revision, note='', model=None).
* reconcile(): returns [{item, current_target}], marking open findings
  needs_recheck when a newer render exists without changing their original anchor.
* handoff(item_ids, enqueue, idempotency_key, *, expected_revisions=None,
  actor='human'): freezes a batch before enqueue(message=..., idempotency_key=...).
  The callback MUST durably deduplicate by key and return a JSON job dictionary.
  Retry the same call after any exception: even if enqueue succeeded before a
  crash, the same frozen message/key recovers its job. No implicit startup dispatch.

Actor/model labels are provenance supplied by the caller, not authentication.
The host must enforce audience and operation permissions at its API boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from vwb.canvas.model import Canvas
from vwb.workspace import Workspace

__all__ = ["QCStore", "QCStoreError", "QCConflict", "QCPinError"]

_OUTPUT = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_TARGET = re.compile(rf"output:({_OUTPUT})@v([1-9][0-9]{{0,8}})")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SCHEMA = "vwb.qc-chat/1"
_HASH_CACHE: OrderedDict[tuple[int, ...], str] = OrderedDict()
_HASH_LOCK = threading.Lock()


class QCStoreError(ValueError):
    """Invalid input, unsafe paths, or damaged private records."""


class QCConflict(QCStoreError):
    """A revision or idempotency key conflicts with an existing record."""


class QCPinError(QCStoreError):
    """A render, committed canvas, or source no longer matches its pin."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _decode(raw: bytes) -> Any:
    def constant(value: str) -> None:
        raise QCStoreError(f"Non-finite JSON value: {value}")

    try:
        return json.loads(raw, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise QCStoreError("Invalid QC or media JSON") from exc


def _text(value: Any, name: str, maximum: int = 256, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise QCStoreError(f"{name} must be a {'possibly empty ' if empty else 'nonempty '}string <= {maximum} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise QCStoreError(f"{name} contains control characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise QCStoreError(f"{name} is not valid UTF-8") from exc
    return value


def _integer(value: Any, name: str, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise QCStoreError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise QCStoreError(f"{name} must be finite and in [{minimum}, {maximum}]")
    return value


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise QCStoreError(f"{name} must be one of {', '.join(choices)}")
    return value


def _id(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise QCStoreError("id must be a canonical UUID") from exc
    return value


def _target_parts(value: Any) -> tuple[str, int]:
    match = _TARGET.fullmatch(value) if isinstance(value, str) else None
    if not match or ".." in match[1]:
        raise QCStoreError("target must be output:<safe-id>@v<positive-version>")
    return match[1], int(match[2])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _open_file(directory: int, name: str, flags: int, *, private: bool = False) -> int:
    try:
        # Concurrent first creation on macOS can return ENOENT even though the
        # competing creator has just published the file. Retry only creation,
        # against the same pinned directory and with every no-follow guard.
        for attempt in range(3):
            try:
                fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
                break
            except FileNotFoundError:
                if not flags & os.O_CREAT or attempt == 2:
                    raise
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise QCStoreError(f"Unsafe or inaccessible file: {name}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (private and info.st_nlink != 1):
        os.close(fd)
        raise QCStoreError(f"Expected regular {'unlinked ' if private else ''}file: {name}")
    return fd


class QCStore:
    def __init__(self, ws: Workspace):
        raw = Path(ws.root).absolute()
        if ".." in raw.parts:
            raise QCStoreError("Workspace path must not contain traversal")
        # The parent is host-selected; macOS /var and /tmp can be system aliases.
        # The workspace itself and every child are opened with O_NOFOLLOW.
        self.root = raw.parent.resolve() / raw.name
        with self._directory(()) as root:
            try:
                os.mkdir(".qc-chat", 0o700, dir_fd=root)
                os.fsync(root)
            except FileExistsError:
                pass
        with self._locked() as directory:
            self._load(directory)

    @contextmanager
    def _directory(self, parts: tuple[str, ...]) -> Iterator[int]:
        fd = None
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise QCStoreError("Unsafe or inaccessible workspace path") from exc
        finally:
            if fd is not None:
                os.close(fd)

    @contextmanager
    def _locked(self, name: str = ".lock") -> Iterator[int]:
        with self._directory((".qc-chat",)) as directory:
            fd = _open_file(directory, name, os.O_RDWR | os.O_CREAT, private=True)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield directory
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _relative(self, authored: Any, prefix: tuple[str, ...]) -> tuple[str, ...]:
        value = _text(authored, "media path", 4096)
        if "\\" in value or any(part in (".", "..", "") for part in value.lstrip("/").split("/")):
            raise QCPinError("Unsafe media path")
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.relative_to(self.root)
            except ValueError as exc:
                raise QCPinError("Media path escapes the parent workspace") from exc
        if path.parts[:len(prefix)] != prefix or len(path.parts) <= len(prefix):
            raise QCPinError(f"Media path must stay under {'/'.join(prefix)}")
        return path.parts

    def _read(self, parts: tuple[str, ...]) -> bytes:
        with self._directory(parts[:-1]) as directory:
            with os.fdopen(_open_file(directory, parts[-1], os.O_RDONLY), "rb") as stream:
                if os.fstat(stream.fileno()).st_size > 32 * 1024 * 1024:
                    raise QCStoreError("Media metadata exceeds 32 MiB")
                return stream.read(32 * 1024 * 1024 + 1)

    def _hash(self, parts: tuple[str, ...], *, fresh: bool) -> str:
        with self._directory(parts[:-1]) as directory:
            with os.fdopen(_open_file(directory, parts[-1], os.O_RDONLY), "rb") as stream:
                identity = _identity(os.fstat(stream.fileno()))
                with _HASH_LOCK:
                    cached = _HASH_CACHE.get(identity)
                    if cached is not None:
                        _HASH_CACHE.move_to_end(identity)
                if fresh or cached is None:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                    cached = hasher.hexdigest()
                if _identity(os.fstat(stream.fileno())) != identity:
                    raise QCPinError("Media changed during verification")
                with _HASH_LOCK:
                    _HASH_CACHE[identity] = cached
                    if len(_HASH_CACHE) > 512:
                        _HASH_CACHE.popitem(last=False)
                return cached

    def _target(self, target: str, *, fresh: bool) -> dict[str, Any]:
        output, version = _target_parts(target)
        try:
            raw = self._read(("canvas", output, f"v{version}.json"))
            data = _decode(raw)
            canvas = Canvas.model_validate(data)
            if (data.get("schema") != "vwb.canvas/2" or data.get("output") != output
                    or type(data.get("version")) is not int or data["version"] != version):
                raise QCPinError("Committed canvas identity does not match target")
            canvas_sha = _digest({k: v for k, v in data.items() if k != "provenance"})
            if data.get("provenance", {}).get("sha256") != canvas_sha:
                raise QCPinError("Committed canvas checksum mismatch")
            fmt = data["format"]
            width = _integer(fmt["width"], "width", 1, 65536)
            height = _integer(fmt["height"], "height", 1, 65536)
            fps = _number(fmt["fps"], "fps", 0.001, 1000)
            for span in data["time_map"]:
                _integer(span["src_in"], "src_in")
                _integer(span["src_out"], "src_out", span["src_in"] + 1)
            if data["time_map"] and data.get("duration_frames") is not None:
                raise QCPinError("Canvas has conflicting clocks")
            frames = _integer(canvas.length, "frames", 1)
            receipt = _decode(self._read(("renders", output, f"v{version}", "receipt.json")))
            if (receipt["output"] != output or type(receipt["version"]) is not int
                    or receipt["version"] != version or receipt["inputs"]["canvas_sha256"] != canvas_sha
                    or _integer(receipt["frames"], "receipt frames", 1) != frames
                    or _number(receipt["fps"], "receipt fps", 0.001, 1000) != fps):
                raise QCPinError("Render receipt disagrees with committed canvas")
            for field, value in (("width", width), ("height", height)):
                if field in receipt and _integer(receipt[field], field, 1, 65536) != value:
                    raise QCPinError(f"Render {field} disagrees with committed canvas")
            file = self._relative(receipt["file"], ("renders", output, f"v{version}"))
            sha = self._hash(file, fresh=fresh)
            if receipt["sha256"] != sha:
                raise QCPinError("Rendered file SHA256 mismatch")
            # Only referenced source videos are pinned; no source targets or work/ paths.
            # pipeline.render currently receipts time_map sources, not arbitrary
            # video-layer refs. Pin that contract, including any extra receipted refs.
            media = receipt["inputs"]["media"]
            if not isinstance(media, dict):
                raise QCPinError("Render media inputs must be an object")
            refs = {sp.media for sp in canvas.time_map} | set(media)
            sources = []
            if refs:
                request = _decode(self._read(("request.json",)))
                available = {f"{s['media_id']}@{s['sha256'][:12]}": s for s in request["sources"]}
                for ref in sorted(refs):
                    source = available[ref]
                    path = self._relative(source["path"], ("media",))
                    digest = self._hash(path, fresh=fresh)
                    if source["sha256"] != digest or media.get(ref) != digest:
                        raise QCPinError("Source file SHA256 mismatch")
                    sources.append({"ref": ref, "sha256": digest, "file": "/".join(path)})
            author_model = canvas.provenance.get("author_model")
            if author_model is not None:
                _text(author_model, "author_model")
            return {"target": target, "output": output, "version": version, "fps": fps,
                    "width": width, "height": height, "frames": frames, "sha256": sha,
                    "file": "/".join(file), "canvas_sha256": canvas_sha,
                    "canvas_file_sha256": hashlib.sha256(raw).hexdigest(),
                    "sources": sources, "author_model": author_model}
        except QCPinError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise QCPinError(f"Cannot verify {target}: {exc}") from exc

    def verify_target(self, target: str) -> dict[str, Any]:
        """Verify committed metadata, render bytes and source bytes without hash cache."""
        return self._target(target, fresh=True)

    def _rendered_versions(self, output: str) -> list[int]:
        versions = []
        try:
            with self._directory(("renders", output)) as directory:
                names = os.listdir(directory)
        except FileNotFoundError:
            return []
        for name in names:
            if re.fullmatch(r"v[1-9][0-9]{0,8}", name):
                with self._directory(("renders", output, name)) as version_dir:
                    try:
                        os.stat("receipt.json", dir_fd=version_dir, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                versions.append(int(name[1:]))
        return sorted(versions, reverse=True)

    def targets(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        """All available versions, newest first, bounded to 1,000 verified targets.

        Unrendered commits are omitted. Corrupt/unsafe receipts fail closed.
        """
        _integer(limit, "limit", 1, 1000)
        candidates = []
        try:
            with self._directory(("renders",)) as renders:
                outputs = os.listdir(renders)
        except FileNotFoundError:
            return []
        for output in sorted(outputs):
            if not re.fullmatch(_OUTPUT, output) or ".." in output:
                continue
            candidates.extend((output, version) for version in self._rendered_versions(output))
        candidates.sort(key=lambda pair: (-pair[1], pair[0]))
        return [self._target(f"output:{out}@v{version}", fresh=False) for out, version in candidates[:limit]]

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema": _SCHEMA, "sequence": 0, "head": None, "items": {}, "batches": {}}

    def _load(self, directory: int) -> dict[str, Any]:
        state = self._empty()
        with os.fdopen(_open_file(directory, "events.jsonl", os.O_RDONLY | os.O_CREAT, private=True), "rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    raise QCStoreError("Incomplete QC journal record; refusing to discard audit data")
                event = _decode(line)
                if not isinstance(event, dict):
                    raise QCStoreError("Invalid QC journal event")
                sha = event.pop("sha256", None)
                if (event.get("sequence") != state["sequence"] + 1 or event.get("previous") != state["head"]
                        or sha != _digest(event) or event.get("schema") != _SCHEMA):
                    raise QCStoreError("QC journal integrity failure")
                self._apply(state, event)
                state.update(sequence=event["sequence"], head=sha)
        try:
            with os.fdopen(_open_file(directory, "items.json", os.O_RDONLY, private=True), "rb") as stream:
                snapshot = _decode(stream.read())
        except FileNotFoundError:
            snapshot = None
        if snapshot != state:
            if state["sequence"] == 0 and snapshot not in (None, self._empty()):
                raise QCStoreError("QC journal missing but snapshot contains data")
            self._write_snapshot(directory, state)
        return state

    @staticmethod
    def _apply(state: dict[str, Any], event: dict[str, Any]) -> None:
        data = event.get("data", {})
        kind = event.get("type")
        if kind in ("item.created", "item.transitioned", "item.recheck"):
            item = data["item"]
            state["items"][item["id"]] = deepcopy(item)
        elif kind in ("handoff.prepared", "handoff.sent"):
            batch = data["batch"]
            state["batches"][batch["idempotency_key"]] = deepcopy(batch)
        else:
            raise QCStoreError("Unknown QC journal event type")

    def _write_snapshot(self, directory: int, state: dict[str, Any]) -> None:
        temp = f".items-{uuid4()}.tmp"
        fd = _open_file(directory, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, private=True)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write((_json(state) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            # Replacement does not follow symlinks; reject them explicitly anyway.
            try:
                existing = _open_file(directory, "items.json", os.O_RDONLY, private=True)
            except FileNotFoundError:
                pass
            else:
                os.close(existing)
            os.replace(temp, "items.json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _append(self, directory: int, state: dict[str, Any], kind: str,
                data: dict[str, Any], actor: str, model: str | None = None) -> None:
        event = {"schema": _SCHEMA, "sequence": state["sequence"] + 1, "previous": state["head"],
                 "id": str(uuid4()), "type": kind, "at": _now(), "actor": actor, "model": model, "data": data}
        sha = _digest(event)
        with os.fdopen(_open_file(directory, "events.jsonl", os.O_WRONLY | os.O_APPEND, private=True), "ab") as stream:
            stream.write((_json({**event, "sha256": sha}) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        self._apply(state, event)
        state.update(sequence=event["sequence"], head=sha)
        self._write_snapshot(directory, state)

    def create(self, payload: dict[str, Any], actor: str = "human", *, model: str | None = None) -> dict[str, Any]:
        required = {"target", "f0", "f1", "expected", "observed"}
        allowed = required | {"region", "category", "priority", "lane", "pin"}
        if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - allowed:
            raise QCStoreError("Finding requires target, f0, f1, expected, observed; unknown fields are forbidden")
        _text(actor, "actor")
        if model is not None:
            _text(model, "model")
        _target_parts(payload["target"])
        pin = payload.get("pin")
        if "pin" in payload:
            if (not isinstance(pin, dict) or set(pin) != {"sha256", "canvas_file_sha256"}
                    or any(not isinstance(value, str) or not _DIGEST.fullmatch(value) for value in pin.values())):
                raise QCStoreError("pin must contain exactly sha256 and canvas_file_sha256 as 64 lowercase hex characters")
            pin = dict(pin)
        f0 = _integer(payload["f0"], "f0")
        f1 = _integer(payload["f1"], "f1", f0 + 1)
        expected = _text(payload["expected"], "expected", 4000)
        observed = _text(payload["observed"], "observed", 4000)
        category = _text(payload.get("category", "visual"), "category", 64)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", category):
            raise QCStoreError("category must be a lowercase identifier")
        priority = _choice(payload.get("priority", "warning"), "priority", ("note", "warning", "blocker"))
        lane = _choice(payload.get("lane", "shared"), "lane", ("shared", "audit"))
        region = payload.get("region")
        if region is not None:
            if not isinstance(region, list) or len(region) != 4:
                raise QCStoreError("region must be [x, y, w, h]")
            x, y, w, h = [_number(v, "region", 0, 1) for v in region]
            if w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
                raise QCStoreError("region must have positive area within the normalized frame")
            region = [x, y, w, h]
        with self._locked() as directory:
            state = self._load(directory)
            target = self.verify_target(payload["target"])
            if pin is not None and any(target[key] != value for key, value in pin.items()):
                raise QCPinError("Inspected pin is stale; inspect the current rendered bytes before creating a finding")
            if f1 > target["frames"]:
                raise QCStoreError("f1 exceeds rendered frame count")
            at = _now()
            item = {"id": str(uuid4()), "revision": 1, "status": "open", "target": target["target"],
                    "f0": f0, "f1": f1, "region": region, "expected": expected, "observed": observed,
                    "category": category, "priority": priority, "lane": lane, "actor": actor, "model": model,
                    "created_at": at, "updated_at": at, "opened_at": at, "closed_at": None,
                    "anchor": {**target, "f0": f0, "f1": f1, "region": region}, "recheck_target": None}
            self._append(directory, state, "item.created", {"item": item}, actor, model)
            return deepcopy(item)

    @staticmethod
    def _visible(state: dict[str, Any], audience: str) -> list[dict[str, Any]]:
        _choice(audience, "audience", ("qc", "editor"))
        if audience == "qc":
            return list(state["items"].values())
        visible = {}
        sent = sorted((b for b in state["batches"].values() if b["status"] == "sent"),
                      key=lambda batch: batch["sent_at"])
        for batch in sent:
            for item in batch["items"]:
                if item["lane"] == "shared":
                    visible[item["id"]] = item
        return list(visible.values())

    def list_items(self, audience: str = "qc") -> list[dict[str, Any]]:
        with self._locked() as directory:
            state = self._load(directory)
            return deepcopy(self._visible(state, audience))

    def get(self, id: str, audience: str = "qc") -> dict[str, Any]:
        _id(id)
        for item in self.list_items(audience):
            if item["id"] == id:
                return item
        raise KeyError(id)

    def transition(self, id: str, status: str, actor: str = "human", *, expected_revision: int,
                   note: str = "", model: str | None = None) -> dict[str, Any]:
        _id(id)
        _choice(status, "status", ("resolved", "dismissed", "open"))
        _integer(expected_revision, "expected_revision", 1)
        _text(actor, "actor")
        _text(note, "note", 4000, empty=True)
        if model is not None:
            _text(model, "model")
        with self._locked() as directory:
            state = self._load(directory)
            item = deepcopy(state["items"][id])
            if item["revision"] != expected_revision:
                raise QCConflict(f"Revision conflict for {id}: expected {expected_revision}, current {item['revision']}")
            at = _now()
            previous = item["status"]
            item.update(status=status, revision=item["revision"] + 1, updated_at=at,
                        closed_at=None if status == "open" else at)
            if status == "open":
                item["opened_at"] = at
            self._append(directory, state, "item.transitioned",
                         {"item": item, "from_status": previous, "note": note}, actor, model)
            return deepcopy(item)

    def _verify_anchor(self, anchor: dict[str, Any]) -> None:
        target = self.verify_target(anchor["target"])
        if any(anchor.get(k) != value for k, value in target.items()):
            raise QCPinError("Original media anchor changed; create a new finding for the new bytes")

    def reconcile(self) -> list[dict[str, Any]]:
        """New renders require reinspection; they never imply a pass or resolution."""
        with self._locked() as directory:
            state = self._load(directory)
            current = {}
            outputs = {item["anchor"]["output"] for item in state["items"].values()
                       if item["status"] in ("open", "needs_recheck")}
            # The UI discovery cap must not hide newer versions of existing findings.
            for output in sorted(outputs):
                versions = self._rendered_versions(output)
                if versions:
                    current[output] = self._target(f"output:{output}@v{versions[0]}", fresh=False)
            candidates = []
            for original in list(state["items"].values()):
                if original["status"] not in ("open", "needs_recheck"):
                    continue
                latest = current.get(original["anchor"]["output"])
                if latest is None or latest["version"] <= original["anchor"]["version"]:
                    continue
                item = deepcopy(original)
                if item["status"] != "needs_recheck" or item["recheck_target"] != latest:
                    item.update(status="needs_recheck", revision=item["revision"] + 1,
                                updated_at=_now(), recheck_target=latest)
                    self._append(directory, state, "item.recheck", {"item": item}, "system:reconcile")
                candidates.append({"item": deepcopy(item), "current_target": deepcopy(latest)})
            return candidates

    @staticmethod
    def _message(batch: dict[str, Any]) -> str:
        lines = [f"Explicit QC handoff {batch['id']}. Review these pinned findings:"]
        for item in batch["items"]:
            # Serialize trusted store fields, never a caller-supplied message or filename.
            lines.append(_json({k: item[k] for k in
                                ("id", "revision", "priority", "category", "expected", "observed", "anchor")}))
        lines.append("A newer render requires reinspection; these findings do not certify a pass.")
        message = "\n".join(lines)
        if len(message) > 100_000:
            raise QCStoreError("Handoff message exceeds 100,000 characters; select fewer items")
        return message

    def handoff(self, item_ids: list[str], enqueue: Callable[..., dict[str, Any]], idempotency_key: str, *,
                expected_revisions: dict[str, int] | None = None, actor: str = "human") -> dict[str, Any]:
        if not isinstance(item_ids, list) or not 1 <= len(item_ids) <= 100:
            raise QCStoreError("handoff requires 1 to 100 item ids")
        ids = sorted(_id(value) for value in item_ids)
        if len(ids) != len(set(ids)):
            raise QCStoreError("Duplicate handoff item ids")
        _text(idempotency_key, "idempotency_key", 240)
        _text(actor, "actor")
        if not callable(enqueue):
            raise QCStoreError("enqueue must be callable")
        if expected_revisions is not None:
            if not isinstance(expected_revisions, dict) or set(expected_revisions) != set(ids):
                raise QCStoreError("expected_revisions must cover exactly the handed-off ids")
            for revision in expected_revisions.values():
                _integer(revision, "expected_revision", 1)
        # Dispatch is serialized separately so the callback can read the store.
        with self._locked(".handoff.lock"):
            with self._locked() as directory:
                state = self._load(directory)
                batch = deepcopy(state["batches"].get(idempotency_key))
                if batch is not None:
                    if (batch["item_ids"] != ids or batch["expected_revisions"] != expected_revisions
                            or batch["actor"] != actor):
                        raise QCConflict("Idempotency key already belongs to a different handoff")
                    if batch["status"] == "sent":
                        return batch
                else:
                    items = [deepcopy(state["items"][id]) for id in ids]
                    for item in items:
                        if item["lane"] == "audit":
                            raise QCStoreError("Private audit findings cannot be handed off")
                        if expected_revisions is not None and expected_revisions[item["id"]] != item["revision"]:
                            raise QCConflict(f"Revision conflict for {item['id']}")
                        self._verify_anchor(item["anchor"])
                    batch = {"id": str(uuid4()), "idempotency_key": idempotency_key, "item_ids": ids,
                             "expected_revisions": deepcopy(expected_revisions), "items": items,
                             "actor": actor, "status": "prepared", "created_at": _now(), "sent_at": None,
                             "job": None}
                    batch["message"] = self._message(batch)
                    self._append(directory, state, "handoff.prepared", {"batch": batch}, actor)
            # Prepared retries intentionally reuse the frozen pins, even if newer
            # media or subsequent private status notes have arrived since dispatch.
            job = enqueue(message=batch["message"], idempotency_key=idempotency_key)
            if not isinstance(job, dict):
                raise QCStoreError("enqueue must return a JSON job dictionary")
            try:
                job = _decode(_json(job).encode())
            except (ValueError, TypeError) as exc:
                raise QCStoreError("enqueue must return a finite JSON job dictionary") from exc
            with self._locked() as directory:
                state = self._load(directory)
                batch.update(status="sent", sent_at=_now(), job=job)
                self._append(directory, state, "handoff.sent", {"batch": batch}, actor)
            return deepcopy(batch)
