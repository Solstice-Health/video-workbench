"""Bounded native model APIs, with provider credentials held by the control plane.

This is deliberately not an arbitrary HTTP proxy. Operation names select fixed
origins/routes; the native payload can use newly released model IDs and options.
One request is one billed invocation, without automatic retries or fallbacks.
Workspace file markers avoid sending large base64 values through agent history.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import stat
import time
import uuid
import wave
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx
from filelock import FileLock

from vwb.config import provider_status, secrets
from vwb.gateway.core import Gateway

MAX_INPUT_JSON = 4 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_INPUT_FILES = 128 * 1024 * 1024
MAX_INPUT_MARKERS = 128
MAX_INPUT_METADATA = 32 * 1024
MAX_RESPONSE_JSON = 128 * 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_ARTIFACTS = 32
BASES = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}
_ID = re.compile(r"[a-zA-Z0-9_.-]{1,250}\Z")
_GOOGLE_OPERATION = re.compile(r"(?:models/[a-zA-Z0-9_.-]+/)?operations/[a-zA-Z0-9_.-]+\Z")
_FILE = re.compile(r"files/[a-zA-Z0-9_.-]+\Z")
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/gif": ".gif", "image/svg+xml": ".svg", "video/mp4": ".mp4",
               "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
               "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac",
               "audio/ogg": ".ogg", "audio/aac": ".aac", "audio/pcm": ".pcm",
               "audio/l16": ".pcm", "application/json": ".json", "text/plain": ".txt"}


@dataclass(frozen=True)
class _Operation:
    route: str
    required: tuple[str, ...] = ("model",)
    transport: str = "json"
    note: str = "Native provider JSON; model availability and options depend on your account."


_SHARED = {
    "chat.completions": _Operation("/chat/completions", ("model", "messages")),
    "responses": _Operation("/responses", ("model", "input"), note="Native Responses JSON. For background=true, retrieve later with responses.status and the returned id."),
    "responses.status": _Operation("/responses/{id}", ("id",), "get"),
    "embeddings": _Operation("/embeddings", ("model", "input")),
    "audio.speech": _Operation("/audio/speech", ("model", "input"), note="Returns an audio artifact. Choose a supported voice and response_format."),
    "audio.transcribe": _Operation("/audio/transcriptions", ("model",), note="OpenAI needs a file marker; OpenRouter accepts input_audio base64 JSON or a file marker."),
    "video.create": _Operation("/videos", ("model", "prompt"), note="Starts a job. Poll video.status with id, then call video.content with id."),
    "video.status": _Operation("/videos/{id}", ("id",), "get"),
    "video.content": _Operation("/videos/{id}/content", ("id",), "get", "Downloads video to the workspace. OpenRouter accepts index; OpenAI accepts variant."),
}
OPERATIONS = {
    "openai": {**_SHARED,
               "images.generate": _Operation("/images/generations", ("model", "prompt")),
               "images.edit": _Operation("/images/edits", ("model", "image", "prompt"), "multipart",
                                          "image and optional mask use {$file: path}; image may be a list of markers.")},
    "openrouter": {**_SHARED,
                   "images.generate": _Operation("/images", ("model", "prompt"), note="Use input_references for edits/reference images when the selected model supports them.")},
    "anthropic": {"messages": _Operation("/messages", ("model", "max_tokens", "messages"))},
    "google": {
        "generate_content": _Operation(":generateContent", ("model", "contents"), note="Text, image, audio and video input/output supported according to the selected model. Use native camelCase REST fields."),
        "embed_content": _Operation(":embedContent", ("model", "content")),
        "batch_embed_contents": _Operation(":batchEmbedContents", ("model", "requests")),
        "predict": _Operation(":predict", ("model", "instances")),
        "predict_long_running": _Operation(":predictLongRunning", ("model", "instances"), note="Starts a job; poll operation.status with the returned name."),
        "operation.status": _Operation("/{name}", ("name",), "get"),
        "file.status": _Operation("/{name}", ("name",), "get"),
        "file.content": _Operation("/{name}:download", ("name",), "get", "Download files/<id> or a returned Gemini files URI. Only the fixed Gemini file endpoint receives credentials."),
    },
}
DOCS = {
    "openai": "https://developers.openai.com/api/reference",
    "openrouter": "https://openrouter.ai/docs/api/api-reference/overview",
    "google": "https://ai.google.dev/api",
    "anthropic": "https://platform.claude.com/docs/en/api/messages/create",
}


class ModelAccessError(RuntimeError):
    """An error safe to expose to the agent; no upstream body or headers."""


@dataclass(frozen=True)
class _Upload:
    name: str
    data: bytes
    mime: str


def _json_bytes(value: Any, maximum: int) -> bytes:
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Model payload must be a finite JSON object.") from exc
    if len(result) > maximum:
        raise ValueError("Model payload exceeds the size limit; use workspace file markers for large inputs.")
    return result


def _redact(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if isinstance(value, dict):
        return {str(k).replace(key, "[redacted]"): _redact(v, key) for k, v in value.items()}
    return value


def _usage(data: Any) -> dict:
    if not isinstance(data, dict):
        return {}
    nested = data.get("response") if isinstance(data.get("response"), dict) else {}
    original = data.get("usage") or data.get("usageMetadata") or nested.get("usage") or nested.get("usageMetadata") or {}
    result = dict(original) if isinstance(original, dict) else {}
    for target, sources in {
        "input_tokens": ("prompt_tokens", "promptTokenCount"),
        "output_tokens": ("completion_tokens", "candidatesTokenCount"),
        "input_tokens_details": ("prompt_tokens_details",),
        "output_tokens_details": ("completion_tokens_details",),
    }.items():
        for source in sources:
            if target not in result and source in result:
                result[target] = result[source]
    return result


def _resource_id(value: Any, name: str = "id") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{name} must be a provider resource ID without URL, slash or query string.")
    return value


def _google_file(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be files/<id> or a Gemini file URI.")
    if value.startswith("https://"):
        url = urlsplit(value)
        if url.netloc != "generativelanguage.googleapis.com" or url.fragment or url.query not in {"", "alt=media"}:
            raise ValueError("Only the fixed Gemini file origin is supported; no arbitrary download URLs.")
        if not url.path.startswith("/v1beta/"):
            raise ValueError("Expected a Gemini /v1beta/files URI.")
        value = url.path.removeprefix("/v1beta/").removesuffix(":download")
    if not _FILE.fullmatch(value) or value.split("/")[-1] in {".", ".."}:
        raise ValueError("name must be a Gemini files/<id> resource.")
    return value


def _error_detail(response, key: str) -> str:
    """Read a small structured error only; never surface headers or raw bodies."""
    chunks, count = [], 0
    for chunk in response.iter_bytes(chunk_size=1024):
        count += len(chunk)
        if count > 8192:
            return ""
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        return ""
    if not isinstance(value, dict):
        return ""
    error = value.get("error", value)
    if isinstance(error, str):
        detail = error
    elif isinstance(error, dict):
        detail = " ".join(f"{name}: {error[name]}" for name in ("message", "code", "param", "status")
                          if isinstance(error.get(name), (str, int)) and not isinstance(error[name], bool))
    else:
        return ""
    # Redact before truncating, so an error boundary cannot expose a key prefix.
    detail = detail.replace(key, "[redacted]")
    detail = re.sub(r"[\x00-\x1f\x7f]", " ", detail)[:1000]
    return f" Provider detail (untrusted): {detail}" if detail else ""


def _google_redirect(current_url: str, location: str, key: str) -> tuple[str, bool]:
    """Permit only Google file/CDN redirects; API credentials never reach a CDN.

    Google documents generated-file downloads with redirect following. Storage
    and googleusercontent are Google's media origins, not arbitrary URLs from
    model prose. The allowlist applies to every hop and never to other APIs.
    """
    target = urljoin(current_url, location)
    url = urlsplit(target)
    try:
        valid_port = url.port in {None, 443}
    except ValueError:
        valid_port = False
    if (url.scheme != "https" or not valid_port or url.username or url.password or url.fragment
            or key in unquote(target) or any(name.lower() in {"key", "api_key", "x-goog-api-key", "authorization"}
                                           for name, _value in parse_qsl(url.query))):
        raise ModelAccessError("Google file redirect was refused; credentials were not forwarded.")
    host = url.hostname or ""
    if host == "generativelanguage.googleapis.com":
        if not re.fullmatch(r"/(?:download/)?v1beta/files/[a-zA-Z0-9_.-]+(?::download)?", url.path):
            raise ModelAccessError("Google file redirect left the permitted file endpoint.")
        return target, True
    if (host in {"storage.googleapis.com", "storage-download.googleapis.com"}
            or re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:storage\.googleapis\.com|googleusercontent\.com)", host)):
        return target, False
    raise ModelAccessError("Google file redirect used an unsupported origin; credentials were not forwarded.")


class ModelAccess:
    """Native provider operations plus live catalogs for one request workspace.

    ``request`` returns provider/operation/model, a sanitized native ``response``,
    usage, ``response_path`` and an ``artifacts`` list. Paths are relative to
    ``ws.work``. Base64 media becomes an artifact reference in the response.
    Catalogs do not make inference requests and never assert account entitlement.
    """

    def __init__(self, ws, actor: str = "model_access", *, deadline: float | None = None,
                 cancel_event=None):
        self.ws = ws
        self.gateway = Gateway(ws, actor)
        self.deadline = deadline
        self.cancel_event = cancel_event

    def _check_cancel(self) -> float:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ModelAccessError("Model request cancelled; check remote job status before resubmitting.")
        remaining = 900 if self.deadline is None else min(900, self.deadline - time.monotonic())
        if remaining <= 0:
            raise ModelAccessError("Model request deadline elapsed; check remote job status before resubmitting.")
        return remaining

    def _account_usage(self, provider: str, operation: str, payload: dict, data: Any,
                       usage: dict) -> tuple[dict, dict]:
        """An async resource reports cumulative usage, not a charge per poll.

        Claims are private to the host and shared by all workers/processes in
        this request. Only final resource usage is claimed; provisional pending
        usage remains in the native result but cannot hide later output tokens.
        """
        resource, kind = None, None
        native = data if isinstance(data, dict) else {}
        if operation in {"responses", "responses.status"}:
            resource = native.get("id") or payload.get("id")
            kind = "response"
        elif operation in {"video.create", "video.status", "video.content"}:
            resource = native.get("id") or payload.get("id")
            kind = "video"
        elif provider == "google" and operation in {"predict_long_running", "operation.status"}:
            resource = native.get("name") or payload.get("name")
            kind = "operation"
        if not usage:
            return {}, {"state": "no_usage"}
        if not kind or not isinstance(resource, str) or not resource or len(resource) > 1000:
            return usage, {"state": "recorded"}
        if native.get("status") in {"queued", "pending", "in_progress", "processing"} or native.get("done") is False:
            return {}, {"state": "pending", "resource": resource}
        directory = self.ws.root / ".model-access" / "usage"
        # This directory is deliberately outside the shared work mount.
        if directory.parent.is_symlink() or directory.is_symlink():
            raise ModelAccessError("Model accounting needs a regular private host directory.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
        digest = hashlib.sha256(f"{provider}:{kind}:{resource}".encode()).hexdigest()
        claim, lock = directory / f"{digest}.json", directory / f"{digest}.lock"
        if claim.is_symlink() or lock.is_symlink():
            raise ModelAccessError("Model accounting files cannot be symbolic links.")
        with FileLock(str(lock)):
            if claim.exists():
                if not claim.is_file():
                    raise ModelAccessError("Model accounting claim is not a regular file.")
                return {}, {"state": "already_recorded", "resource": resource}
            content = json.dumps({"provider": provider, "kind": kind, "resource": resource,
                                  "usage_sha256": hashlib.sha256(_json_bytes(usage, MAX_RESPONSE_JSON)).hexdigest()}).encode()
            fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
        return usage, {"state": "recorded", "resource": resource}

    @staticmethod
    def providers() -> dict:
        return {
            "items": [{**row, "operations": [{"name": name, "required": list(op.required),
                                               "transport": op.transport, "note": op.note}
                                              for name, op in OPERATIONS[row["id"]].items()],
                       "docs": DOCS[row["id"]]} for row in provider_status()],
            "file_marker": {"$file": "relative/path/in/work", "encoding": "data_url|base64|text"},
            "note": "Credentials stay on the host. Catalogs are provider reports, not a guarantee of account access. Use native API options; no automatic retries. Multipart file markers omit encoding. Live streaming APIs are not exposed by this batch gateway.",
        }

    def _key(self, provider: str) -> str:
        if provider not in BASES:
            raise ValueError("Choose openai, google, anthropic or openrouter.")
        return secrets().require(provider)

    @staticmethod
    def _headers(provider: str, key: str) -> dict[str, str]:
        if provider == "google":
            return {"x-goog-api-key": key}
        if provider == "anthropic":
            return {"x-api-key": key, "anthropic-version": "2023-06-01"}
        return {"authorization": f"Bearer {key}"}

    def _work_fd(self) -> int:
        try:
            self.ws.work.mkdir(parents=True, exist_ok=True)
            return os.open(self.ws.work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ModelAccessError("The request work folder must be a regular local directory without symbolic links.") from exc

    def _read_file(self, name: Any) -> _Upload:
        if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 4096
                or "\x00" in name or Path(name).is_absolute() or "\\" in name):
            raise ValueError("File markers must use paths relative to the request work folder.")
        parts = name.split("/")
        if any(p in {"", ".", ".."} or p.startswith(".env") or p in {".keys", ".git"} for p in parts):
            raise ValueError("File markers cannot escape work or read credential/metadata files.")
        fd = self._work_fd()
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ValueError("File markers need regular files of at most 64 MiB.")
                data = handle.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ValueError("File marker input exceeds 64 MiB.")
        except OSError as exc:
            raise ValueError("File marker cannot be read; check the path and avoid symbolic links.") from exc
        finally:
            os.close(fd)
        return _Upload(parts[-1], data, mimetypes.guess_type(parts[-1])[0] or "application/octet-stream")

    def _expand(self, value: Any, budget: list[int], depth: int = 0, inputs: list[dict] | None = None) -> Any:
        if depth > 50:
            raise ValueError("Model payload nesting exceeds 50 levels.")
        if isinstance(value, dict):
            if "$file" in value:
                if set(value) - {"$file", "encoding", "mime_type"}:
                    raise ValueError("A file marker accepts only $file, encoding and optional mime_type.")
                while len(budget) < 3:
                    budget.append(0)
                budget[1] += 1
                budget[2] += len(str(value["$file"]).encode("utf-8")) + 200
                if budget[1] > MAX_INPUT_MARKERS or budget[2] > MAX_INPUT_METADATA:
                    raise ValueError("A model request accepts at most 128 file markers and 32 KiB of input metadata.")
                upload = self._read_file(value["$file"])
                budget[0] += len(upload.data)
                if budget[0] > MAX_INPUT_FILES:
                    raise ValueError("Combined model file inputs exceed 128 MiB.")
                encoding = value.get("encoding")
                mime = value.get("mime_type", upload.mime)
                if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime):
                    raise ValueError("File marker mime_type must be a plain MIME type.")
                if inputs is not None:
                    inputs.append({"path": value["$file"], "sha256": hashlib.sha256(upload.data).hexdigest(),
                                   "bytes": len(upload.data), "mime_type": mime})
                if encoding is None:
                    return _Upload(upload.name, upload.data, mime)
                if encoding == "text":
                    try:
                        return upload.data.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError("A text file marker must contain UTF-8 text.") from exc
                if encoding not in {"base64", "data_url"}:
                    raise ValueError("File marker encoding must be base64, data_url or text; omit it for multipart uploads.")
                encoded = base64.b64encode(upload.data).decode("ascii")
                return f"data:{mime};base64,{encoded}" if encoding == "data_url" else encoded
            return {k: self._expand(v, budget, depth + 1, inputs) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand(v, budget, depth + 1, inputs) for v in value]
        return value

    def _output_dir(self) -> tuple[int, str]:
        fd = self._work_fd()
        relative = f"model-output/{uuid.uuid4().hex}"
        try:
            for part in relative.split("/"):
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                # work/ is intentionally shared with Docker UID 1001. Also
                # upgrade a model-output root created by an older private-mode
                # gateway, and do not depend on the host's umask.
                os.fchmod(fd, 0o755)
            return fd, relative
        except OSError as exc:
            os.close(fd)
            raise ModelAccessError("Model output folder is unavailable; remove symbolic links from work/model-output.") from exc

    def _save(self, fd: int, relative: str, name: str, content: bytes, mime: str) -> dict:
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ModelAccessError("Model output artifact exceeds 256 MiB.")
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        os.fchmod(out, 0o644)
        with os.fdopen(out, "wb") as handle:
            handle.write(content)
        return {"path": f"{relative}/{name}", "mime_type": mime, "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest()}

    def _media(self, data: Any, fd: int, relative: str, artifacts: list[dict], payload: dict,
               parent: str = "", depth: int = 0) -> Any:
        if depth > 70:
            raise ModelAccessError("Provider response nesting exceeds the limit.")
        if isinstance(data, list):
            return [self._media(v, fd, relative, artifacts, payload, parent, depth + 1) for v in data]
        if isinstance(data, dict):
            result = dict(data)
            encoded, mime, target = None, "", ""
            if isinstance(data.get("b64_json"), str):
                encoded, target = data["b64_json"], "b64_json"
                mime = data.get("media_type") or data.get("mime_type") or f"image/{payload.get('output_format', 'png')}"
            elif parent in {"inlineData", "inline_data"} and isinstance(data.get("data"), str):
                encoded, target = data["data"], "data"
                mime = data.get("mimeType") or data.get("mime_type") or "application/octet-stream"
            elif parent in {"audio", "output_audio", "outputAudio", "output_video", "outputVideo"} and isinstance(data.get("data"), str):
                encoded, target = data["data"], "data"
                fmt = (payload.get("audio") or {}).get("format", "pcm16") if isinstance(payload.get("audio", {}), dict) else "pcm16"
                mime = data.get("mimeType") or data.get("mime_type") or ("video/mp4" if "video" in parent.lower() else {"mp3": "audio/mpeg", "pcm16": "audio/pcm"}.get(fmt, f"audio/{fmt}"))
            if encoded is not None:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ModelAccessError("Provider returned invalid base64 media.") from exc
                artifact = self._save_media(fd, relative, raw, mime, artifacts)
                result[target] = {"artifact": artifact["path"]}
            for key, value in result.items():
                if key != target:
                    result[key] = self._media(value, fd, relative, artifacts, payload, key, depth + 1)
            return result
        if isinstance(data, str) and re.match(r"data:(?:image|audio|video)/", data):
            header, sep, encoded = data.partition(",")
            if not sep or not header.endswith(";base64"):
                return data
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ModelAccessError("Provider returned an invalid media data URL.") from exc
            artifact = self._save_media(fd, relative, raw, header[5:-7], artifacts)
            return {"artifact": artifact["path"]}
        return data

    def _save_media(self, fd: int, relative: str, raw: bytes, mime: str, artifacts: list[dict]) -> dict:
        self._check_cancel()
        if len(artifacts) >= MAX_ARTIFACTS:
            raise ModelAccessError("Provider returned more than 32 media artifacts.")
        if not raw:
            raise ModelAccessError("Provider returned empty media.")
        if not isinstance(mime, str) or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+(?:;[^\r\n]*)?", mime):
            raise ModelAccessError("Provider returned an invalid media MIME type.")
        kind = mime.partition(";")[0].lower()
        if kind == "audio/l16" and (rate := re.search(r"(?:^|;)\s*rate=(\d+)", mime, re.I)):
            sample_rate = int(rate[1])
            if not 1000 <= sample_rate <= 192000 or len(raw) % 2:
                raise ModelAccessError("Provider PCM audio has invalid sample metadata.")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(raw)
            raw, mime, kind = buffer.getvalue(), "audio/wav", "audio/wav"
        suffix = _EXTENSIONS.get(kind, mimetypes.guess_extension(kind) or ".bin")
        artifact = self._save(fd, relative, f"output-{len(artifacts) + 1:02d}{suffix}", raw, mime)
        if suffix == ".pcm":
            artifact["note"] = "Raw PCM; use the selected provider/model's documented sample rate, channels and byte order."
        artifacts.append(artifact)
        return artifact

    def _http(self, provider: str, key: str, method: str, route: str, *, params: dict | None = None,
              body: dict | None = None, multipart: bool = False, binary: bool = False,
              fd: int | None = None, relative: str = "", payload: dict | None = None) -> tuple[Any, list[dict]]:
        kwargs: dict[str, Any] = {"headers": self._headers(provider, key), "params": params}
        if multipart:
            _fields, files = self._multipart(body or {})
            kwargs.update(data={}, files=files or [("_multipart", (None, ""))])
        elif body is not None:
            kwargs["content"] = _json_bytes(body, MAX_INPUT_FILES * 2)
            kwargs["headers"]["content-type"] = "application/json"
        try:
            remaining = self._check_cancel()
            with httpx.Client(timeout=httpx.Timeout(remaining, connect=min(30, remaining)), follow_redirects=False, trust_env=False) as client:
                with ExitStack() as stack:
                    response = stack.enter_context(client.stream(method, BASES[provider] + route, **kwargs))
                    self._check_cancel()
                    for _redirect in range(3):
                        if not response.is_redirect or not (provider == "google" and binary and route.endswith(":download")):
                            break
                        target, send_key = _google_redirect(str(response.url), response.headers.get("location", ""), key)
                        response.close()
                        self._check_cancel()
                        response = stack.enter_context(client.stream("GET", target,
                            headers=self._headers(provider, key) if send_key else {}))
                    if response.is_redirect:
                        raise ModelAccessError(f"{provider} redirected the API request; credentials were not forwarded.")
                    if response.status_code >= 400:
                        hint = {401: "Check the configured key.", 402: "Check account credits.",
                                403: "Check account/model access.", 404: "Check the live catalog and resource ID.",
                                429: "Wait or choose a model within the account's rate limits."}.get(response.status_code, "Check the provider API options and status.")
                        detail = _error_detail(response, key)
                        raise ModelAccessError(f"{provider} returned HTTP {response.status_code}. {hint}{detail}")
                    mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].lower()
                    is_json = mime == "application/json" or mime.endswith("+json")
                    if binary and not is_json:
                        if fd is None:
                            raise ModelAccessError("Missing output workspace.")
                        artifact = self._save_binary_stream(response, fd, relative, mime, payload or {})
                        return {"artifact": artifact["path"]}, [artifact]
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        self._check_cancel()
                        size += len(chunk)
                        if size > MAX_RESPONSE_JSON:
                            raise ModelAccessError("Provider response exceeds 128 MiB.")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    if is_json:
                        try:
                            result = json.loads(raw)
                        except (ValueError, RecursionError) as exc:
                            raise ModelAccessError("Provider returned invalid JSON.") from exc
                        if isinstance(result, dict) and result.get("error"):
                            raise ModelAccessError(f"{provider} reported a request error; no automatic retry was made.")
                        return _redact(result, key), []
                    if mime.startswith("text/"):
                        return {"text": raw.decode("utf-8", errors="replace").replace(key, "[redacted]")}, []
                    raise ModelAccessError("Provider returned an unsupported response content type.")
        except httpx.HTTPError as exc:
            raise ModelAccessError(f"{provider} request failed or timed out. No automatic retry was made; check job status before resubmitting.") from exc

    def _save_binary_stream(self, response, fd: int, relative: str, mime: str, payload: dict) -> dict:
        if not mime.startswith(("image/", "audio/", "video/")) and mime != "application/octet-stream":
            raise ModelAccessError("Provider did not return a media response.")
        # The speech endpoint may use octet-stream; a requested format supplies
        # an extension without asserting that an unknown stream is playable.
        fmt = payload.get("response_format", "")
        suffix = _EXTENSIONS.get(mime, {"mp3": ".mp3", "wav": ".wav", "pcm": ".pcm", "opus": ".opus", "aac": ".aac", "flac": ".flac"}.get(fmt, ".bin"))
        name = f"output-01{suffix}"
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        os.fchmod(out, 0o644)
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(out, "wb") as handle:
                for chunk in response.iter_bytes():
                    self._check_cancel()
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise ModelAccessError("Model output artifact exceeds 256 MiB.")
                    digest.update(chunk)
                    handle.write(chunk)
            if not size:
                raise ModelAccessError("Provider returned an empty media file.")
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise
        result = {"path": f"{relative}/{name}", "mime_type": mime, "bytes": size, "sha256": digest.hexdigest()}
        if suffix == ".pcm":
            result["note"] = "Raw PCM; sample rate/channels/byte order depend on the selected model."
        return result

    @staticmethod
    def _multipart(body: dict) -> tuple[list[tuple[str, str]], list[tuple[str, tuple]]]:
        fields, files = [], []
        for key, value in body.items():
            if isinstance(value, _Upload):
                files.append((key, (value.name, value.data, value.mime)))
            elif isinstance(value, list) and value and all(isinstance(v, _Upload) for v in value):
                files.extend((key if key.endswith("[]") else key + "[]", (v.name, v.data, v.mime)) for v in value)
            elif isinstance(value, (dict, list)):
                if isinstance(value, list) and all(isinstance(v, str) for v in value) and key.endswith("[]"):
                    fields.extend((key, v) for v in value)
                else:
                    fields.append((key, json.dumps(value, allow_nan=False)))
            elif value is not None:
                fields.append((key, str(value).lower() if isinstance(value, bool) else str(value)))
        # httpx's multipart encoder accepts a dict for fields; repeated fields
        # are encoded as (None, value) parts to retain [] arrays.
        files[:0] = [(name, (None, value)) for name, value in fields]
        return [], files

    def request(self, provider: str, operation: str, payload: dict, purpose: str = "model request") -> dict:
        key = self._key(provider)
        if operation not in OPERATIONS[provider]:
            raise ValueError(f"Unsupported {provider} operation. Call model_providers for supported operations.")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a native provider JSON object.")
        _json_bytes(payload, MAX_INPUT_JSON)
        if payload.get("stream") or payload.get("stream_format"):
            raise ValueError("Streaming is not supported here; request a bounded batch response.")
        if payload.get("tools") or payload.get("plugins") or payload.get("mcp_servers"):
            raise ValueError("Provider-hosted tools/plugins are unavailable here; use Workbench Registry tools for web, code, generation and MCP access.")
        if any(k.lower() in {"authorization", "headers", "api_key", "base_url", "url", "endpoint"} for k in payload):
            raise ValueError("Credentials, URLs and headers are host-owned; use a supported operation.")
        if purpose == "":
            purpose = "model request"
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 1000:
            raise ValueError("purpose must explain the request in 1–1000 characters.")
        spec = OPERATIONS[provider][operation]
        for field in spec.required:
            if field not in payload or payload[field] is None or payload[field] == "":
                raise ValueError(f"{operation} requires payload.{field}.")
        inputs: list[dict] = []
        body = self._expand(payload, [0], inputs=inputs)
        logged_request = {"payload": payload, "inputs": inputs}
        model = body.get("model", "")
        if model and (not isinstance(model, str) or len(model) > 300):
            raise ValueError("model must be a provider model ID of at most 300 characters.")
        route, method, params = spec.route, "POST", None
        if spec.transport == "get":
            method = "GET"
            if operation.startswith(("video.", "responses.")):
                route = route.replace("{id}", _resource_id(body["id"]))
                supported = set() if operation.startswith("responses.") else {"index"} if provider == "openrouter" else {"variant"}
                if set(body) - ({"id"} | supported):
                    raise ValueError(f"{operation} accepts id and {', '.join(sorted(supported))} only.")
                params = {k: body[k] for k in supported if k in body}
                if "index" in body and (type(body["index"]) is not int or not 0 <= body["index"] <= 100):
                    raise ValueError("index must be an integer from 0 to 100.")
                if "variant" in body and body["variant"] not in {"video", "thumbnail", "spritesheet"}:
                    raise ValueError("variant must be video, thumbnail or spritesheet.")
            else:
                if set(body) != {"name"}:
                    raise ValueError(f"{operation} accepts only name.")
                name = body["name"]
                if operation == "operation.status":
                    if not isinstance(name, str) or not _GOOGLE_OPERATION.fullmatch(name) or ".." in name.split("/"):
                        raise ValueError("name must be a returned Gemini operations resource name.")
                else:
                    name = _google_file(name)
                route = route.replace("{name}", name)
                if operation == "file.content":
                    params = {"alt": "media"}
            body = None
        elif provider == "google":
            name = model.removeprefix("models/")
            _resource_id(name, "model")
            route = "/models/" + name + route
            body = {k: v for k, v in body.items() if k != "model"}
            # embedContent's native schema requires its model field as well as
            # the route; other model methods do not accept that field.
            if operation == "embed_content":
                body["model"] = "models/" + name
        multipart = spec.transport == "multipart" or (provider == "openai" and operation in {"audio.transcribe", "video.create"})
        if provider == "openrouter" and operation == "audio.transcribe" and isinstance((body or {}).get("file"), _Upload):
            multipart = True
        binary = operation in {"audio.speech", "video.content", "file.content"}
        fd, relative = self._output_dir()
        t0, usage, accounted_usage = time.monotonic(), {}, {}
        try:
            data, artifacts = self._http(provider, key, method, route, params=params, body=body,
                                         multipart=multipart, binary=binary, fd=fd, relative=relative, payload=payload)
            usage = _usage(data)
            accounted_usage, accounting = self._account_usage(provider, operation, payload, data, usage)
            data = self._media(data, fd, relative, artifacts, payload)
            result = {"provider": provider, "operation": operation, "model": model, "response": data,
                      "usage": usage, "artifacts": artifacts, "inputs": inputs, "accounting": accounting}
            saved = self._save(fd, relative, "response.json", _json_bytes(result, MAX_RESPONSE_JSON), "application/json")
            result["response_path"] = saved["path"]
            self.gateway.record(slot_name="model.access", provider=provider, model=model or "(resource)",
                                purpose=purpose, request=logged_request, response_text=json.dumps(result, ensure_ascii=False),
                                usage=accounted_usage, seconds=time.monotonic() - t0, ok=True)
            return result
        except Exception as exc:
            safe = str(exc).replace(key, "[redacted]") if isinstance(exc, (ModelAccessError, ValueError)) else "Model request failed while processing the response."
            self.gateway.record(slot_name="model.access", provider=provider, model=model or "(resource)",
                                purpose=purpose, request=logged_request, response_text="", usage=accounted_usage,
                                seconds=time.monotonic() - t0, ok=False, error=safe)
            if isinstance(exc, (ModelAccessError, ValueError)):
                raise
            raise ModelAccessError(safe) from exc
        finally:
            os.close(fd)

    def catalog(self, provider: str, query: str = "", modality: str = "", limit: int = 30, offset: int = 0) -> dict:
        key = self._key(provider)
        if not isinstance(query, str) or len(query) > 300 or not isinstance(modality, str):
            raise ValueError("Use a short query and a modality string.")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError("limit must be 1–100 and offset 0–100000.")
        if modality not in {"", "all", "text", "image", "audio", "video", "embedding", "embeddings", "speech", "transcription"}:
            raise ValueError("Unsupported modality filter.")
        route, params = "/models", {}
        if provider == "openrouter":
            if modality in {"image", "video"}:
                route = "/images/models" if modality == "image" else "/videos/models"
            else:
                params = {"output_modalities": "embeddings" if modality == "embedding" else modality or "all"}
        elif provider == "google":
            params = {"pageSize": 1000}
        elif provider == "anthropic":
            params = {"limit": 1000}
        t0, rows, more = time.monotonic(), [], False
        try:
            for _ in range(10):
                data, _artifacts = self._http(provider, key, "GET", route, params=params)
                if not isinstance(data, dict):
                    raise ModelAccessError("Provider returned a malformed model catalog.")
                page = data.get("models" if provider == "google" else "data", [])
                if not isinstance(page, list):
                    raise ModelAccessError("Provider returned a malformed model list.")
                rows.extend(v for v in page if isinstance(v, dict))
                if provider == "google" and data.get("nextPageToken"):
                    params["pageToken"], more = data["nextPageToken"], True
                elif provider == "anthropic" and data.get("has_more") and data.get("last_id"):
                    params["after_id"], more = data["last_id"], True
                else:
                    more = bool(data.get("has_more"))
                    break
            needle = query.casefold()
            items = []
            for row in rows:
                if needle and needle not in " ".join(str(row.get(k, "")) for k in ("id", "name", "display_name", "displayName", "description")).casefold():
                    continue
                methods = row.get("supportedGenerationMethods", [])
                if provider == "google" and modality in {"embedding", "embeddings"} and not any("embed" in str(m).lower() for m in methods):
                    continue
                # Do not invent modality metadata from model ID substrings.
                # Preserve native capability fields, including newly introduced
                # video durations/resolutions/reference limits and image options.
                selected = dict(row)
                if isinstance(selected.get("description"), str):
                    selected["description"] = selected["description"][:1500]
                items.append(selected)
            result = {"provider": provider, "items": items[offset:offset + limit], "offset": offset,
                      "next_offset": offset + limit if offset + limit < len(items) else None,
                      "total_matching": len(items), "provider_catalog_truncated": more,
                      "modality_metadata": "provider_reported" if provider == "openrouter" else "generation_methods_only" if provider == "google" else "not_advertised",
                      "modality_filter_applied": provider == "openrouter" or (provider == "google" and modality in {"embedding", "embeddings"}),
                      "note": "Live provider catalog; listings do not guarantee account entitlement, region, credit or model availability. Where modality is not advertised, results are unfiltered; inspect provider docs before choosing."}
            self.gateway.record(slot_name="model.catalog", provider=provider, model="(catalog)", purpose="Discover model capabilities", request={"query": query, "modality": modality, "offset": offset, "limit": limit}, response_text=json.dumps(result), usage={}, seconds=time.monotonic() - t0, ok=True)
            return result
        except Exception as exc:
            self.gateway.record(slot_name="model.catalog", provider=provider, model="(catalog)", purpose="Discover model capabilities", request={"query": query, "modality": modality}, response_text="", usage={}, seconds=time.monotonic() - t0, ok=False, error=str(exc).replace(key, "[redacted]") if isinstance(exc, ModelAccessError) else "Catalog processing failed")
            if isinstance(exc, ModelAccessError):
                raise
            raise ModelAccessError("Provider model catalog could not be processed.") from exc
