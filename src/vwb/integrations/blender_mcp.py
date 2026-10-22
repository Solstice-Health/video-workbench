"""A pinned local MCP bridge to the user's Blender session.

Only this known bridge may be launched; neither agents nor connection settings
can supply a command. The official MCP SDK owns stdio and process teardown. An
explicit host-side preparation step installs hash-locked dependencies in an
isolated environment. It never edits Blender preferences or other MCP clients.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import httpx
from filelock import FileLock, Timeout
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from vwb.config import RUNS_DIR

from .artifacts import import_result
from .remote_mcp import MAX_BYTES, _clean_catalog
from .store import IntegrationError, Store

VERSION = "2.2.2"
PREFIX = "blender_mcp."
WHEEL_NAME = "mcp_for_blender-2.2.2-py3-none-any.whl"
WHEEL_URL = ("https://files.pythonhosted.org/packages/50/89/10eeb36f86cf29d02890e211e093c8be7205cb92ec9c41e91e22d1ceab10/"
             + WHEEL_NAME)
WHEEL_SHA256 = "7dfaadb509b0cc8902367a0b327f8ed4df1c06a8721bf0c1aba2a96c82717e0d"
LOCK = Path(__file__).with_name("blender-runtime.lock")
READ_TOOLS = frozenset({"get_addon_status", "get_scene_info", "look", "search_mentions"})
CODE_TOOL = "execute_blender_code"
SETUP = ("Prepare the Blender bridge, then in Blender choose Edit → Preferences → Add-ons → Install from Disk "
         "and select the prepared addon.py. Enable MCP for Blender, open its N-panel and start its MCP server "
         "on 127.0.0.1. Keep Blender open. Only one client should control this addon at a time.")
TRUST = ("Blender Python runs with your desktop account's file and network access. "
         "Enable it only for trusted work; save a separate .blend before editing. "
         "Cloud asset and paid generation tools are not exposed by this preset.")
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".exr", ".hdr", ".tga"})


def _json_size(value, limit: int, label: str) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError):
        raise IntegrationError(f"{label} must contain valid JSON values.") from None
    if size > limit:
        raise IntegrationError(f"{label} exceeded its size limit. Narrow the request.")


def _tool_error(result: dict) -> bool:
    # Upstream 2.2.2 sometimes reports an execution/connection failure as text
    # without setting MCP isError. Preserve that evidence and mark the call failed.
    return bool(result.get("isError")) or any(
        block.get("type") == "text" and isinstance(block.get("text"), str)
        and block["text"].lstrip().startswith(("Error:", "Error ", "Rejected by safe mode", "Could not determine addon status."))
        for block in result.get("content", []) if isinstance(block, dict)
    )


class DesktopBlender:
    def __init__(self, runs_dir: Path = RUNS_DIR):
        self.store = Store(runs_dir, "blender_mcp")
        self.runtime = self.store.root / "runtime" / VERSION

    def _directory(self, path: Path) -> Path:
        try:
            relative = path.relative_to(self.store.root)
            current = self.store.root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise IntegrationError("Blender bridge storage must not contain symlinks.")
                current.mkdir(mode=0o700, exist_ok=True)
            return current
        except OSError:
            raise IntegrationError("Could not create private Blender bridge storage.") from None

    @property
    def python(self) -> Path:
        return self.runtime / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _prepared(self) -> bool:
        manifest = self.runtime / "manifest.json"
        try:
            if any(path.is_symlink() for path in (self.runtime.parent, self.runtime, self.runtime / "venv",
                                                  self.python.parent, manifest)):
                return False
            data = json.loads(manifest.read_text())
            addon = self.runtime / "addon.py"
            return (data.get("version") == VERSION and data.get("wheel_sha256") == WHEEL_SHA256
                    and data.get("lock_sha256") == hashlib.sha256(LOCK.read_bytes()).hexdigest()
                    and addon.is_file() and not addon.is_symlink()
                    and hashlib.sha256(addon.read_bytes()).hexdigest() == data.get("addon_sha256")
                    and self.python.is_file())
        except (OSError, ValueError):
            return False

    def status(self) -> dict:
        saved = self.store.read()
        prepared = self._prepared()
        connected = bool(prepared and saved.get("enabled") and saved.get("verified_at") and not saved.get("error"))
        return {"id": "blender_mcp", "name": "Blender MCP", "kind": "mcp",
                "description": "Inspect Blender scenes and viewport images; optionally run Blender Python to create, animate and render.",
                "state": "connected" if connected else "error" if saved.get("error") else "disconnected",
                "available": True, "enabled": bool(saved.get("enabled")), "connected": connected,
                "prepared": prepared, "bridge_version": VERSION,
                "addon_path": str(self.runtime / "addon.py") if prepared else None,
                "port": saved.get("port", 9876), "allow_code_execution": bool(saved.get("allow_code_execution")),
                "tools_count": len(self._permitted(saved.get("tools", []), saved)) if connected else 0,
                "message": saved.get("error") or ("Connected to Blender. Save edits to a workspace path and inspect the viewport after changes."
                                                   if connected else SETUP),
                "trust_notice": TRUST, "updated_at": saved.get("verified_at"), "expires_at": None,
                "persistent": False}

    def configure(self, allow_code_execution: bool | None = None, port: int | None = None) -> dict:
        if ((allow_code_execution is not None and type(allow_code_execution) is not bool)
                or (port is not None and (type(port) is not int or not 1024 <= port <= 65535))):
            raise IntegrationError("Choose a local Blender port from 1024–65535 and an explicit Python permission.")
        with self.store.lock:
            saved = self.store.read()
            port = saved.get("port", 9876) if port is None else port
            allow_code_execution = bool(saved.get("allow_code_execution")) if allow_code_execution is None else allow_code_execution
            if port != saved.get("port", 9876):
                saved.pop("verified_at", None)
                saved["tools"] = []
            saved.update(port=port, allow_code_execution=allow_code_execution, generation=secrets.token_hex(16))
            saved.pop("error", None)
            self.store.write(saved)
        return self.status()

    def _environment(self, port: int) -> dict[str, str]:
        # The MCP SDK copies a small default environment; override all of those
        # entries as well, so arbitrary PATH plugins, shell init and vendor keys
        # cannot reach the bridge. This is process hygiene, not a Python sandbox.
        private = self._directory(self.runtime / "home")
        temporary = self._directory(self.runtime / "tmp")
        return {"HOME": str(private), "USER": "workbench", "LOGNAME": "workbench", "SHELL": "/bin/sh",
                "PATH": "/usr/bin:/bin", "TERM": "dumb", "LANG": "en_US.UTF-8",
                "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
                "XDG_CACHE_HOME": str(private / ".cache"), "XDG_DATA_HOME": str(private / ".local/share"),
                "BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": str(port),
                "DISABLE_TELEMETRY": "true", "BLENDER_MCP_DISABLE_TELEMETRY": "true",
                "MCP_DISABLE_TELEMETRY": "true", "PYTHONNOUSERSITE": "1", "UV_NO_CONFIG": "1"}

    def _parameters(self, saved: dict) -> StdioServerParameters:
        if not self._prepared():
            raise IntegrationError("Prepare the pinned Blender bridge in Integrations first. " + SETUP)
        return StdioServerParameters(command=str(self.python), args=["-I", "-m", "blender_mcp.server"],
                                     env=self._environment(saved.get("port", 9876)), cwd=self.runtime)

    def prepare(self) -> dict:
        """Human settings action only. Fetch package data; never install an addon into Blender."""
        lock = self.store.root / "operation.lock"
        if lock.is_symlink():
            raise IntegrationError("Blender bridge lock must not be a symlink.")
        try:
            with FileLock(lock, timeout=1):
                if self._prepared():
                    return self.status()
                self._directory(self.runtime)
                if (self.runtime / "venv").is_symlink():
                    raise IntegrationError("Blender bridge storage must not contain symlinks.")
                uv = shutil.which("uv")
                if uv is None:
                    candidate = Path.home() / ".local/bin/uv"
                    uv = str(candidate) if candidate.is_file() else None
                if uv is None:
                    raise IntegrationError("Install uv before preparing the Blender bridge. See docs/SETUP.md.")
                raw = bytearray()
                with httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False, trust_env=False) as client:
                    with client.stream("GET", WHEEL_URL) as response:
                        if response.status_code != 200:
                            raise IntegrationError("Could not download the pinned Blender bridge. Try Prepare again.")
                        for chunk in response.iter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 8 * 1024 * 1024:
                                raise IntegrationError("The Blender bridge download exceeded its size limit.")
                if hashlib.sha256(raw).hexdigest() != WHEEL_SHA256:
                    raise IntegrationError("Blender bridge checksum mismatch. No downloaded code was installed.")
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    addon = archive.read("blender_mcp/bundled/addon.py")
                    licence = archive.read("mcp_for_blender-2.2.2.dist-info/licenses/LICENSE")
                # Copy known individual members, never extract archive paths.
                for name, content in (("addon.py", addon), ("LICENSE", licence)):
                    path = self.runtime / name
                    if path.is_symlink():
                        raise IntegrationError("Blender bridge storage must not contain symlinks.")
                    path.write_bytes(content)
                environment = self._environment(9876)
                environment["UV_CACHE_DIR"] = str(self._directory(self.runtime / "uv-cache"))
                commands = [[uv, "venv", "--clear", "--python", sys.executable, str(self.runtime / "venv")],
                            [uv, "pip", "sync", "--python", str(self.python), "--require-hashes",
                             "--only-binary", ":all:", str(LOCK)]]
                for command in commands:
                    completed = subprocess.run(command, cwd=self.runtime, env=environment, capture_output=True,
                                               timeout=300, check=False)
                    if completed.returncode:
                        raise IntegrationError("The pinned Blender bridge environment could not be prepared. "
                                               "Check uv, Python 3.12 and network access, then try Prepare again.")
                value = {"version": VERSION, "wheel_sha256": WHEEL_SHA256,
                         "addon_sha256": hashlib.sha256(addon).hexdigest(),
                         "lock_sha256": hashlib.sha256(LOCK.read_bytes()).hexdigest()}
                manifest = self.runtime / "manifest.json"
                if manifest.is_symlink():
                    raise IntegrationError("Blender bridge storage must not contain symlinks.")
                manifest.write_text(json.dumps(value))
        except Timeout:
            raise IntegrationError("Another Blender operation is running. Wait for it before preparing the bridge.") from None
        except (httpx.HTTPError, subprocess.SubprocessError, OSError, zipfile.BadZipFile, KeyError):
            raise IntegrationError("Could not prepare the pinned Blender bridge. Check uv and network access, then try again.") from None
        return self.status()

    @staticmethod
    def _permitted(rows: list[dict], saved: dict) -> list[dict]:
        allowed = READ_TOOLS | ({CODE_TOOL} if saved.get("allow_code_execution") else set())
        return [row for row in rows if row["name"].removeprefix(PREFIX) in allowed]

    def _current(self, generation: str) -> dict:
        saved = self.store.read()
        if not saved.get("enabled") or saved.get("generation") != generation:
            raise IntegrationError("The Blender connection or permissions changed. Reconnect and try again.")
        return saved

    async def _operation(self, saved: dict, *, remote_name=None, arguments=None, read_only=True,
                         native_image=None) -> tuple[list[dict], dict | None]:
        parameters = self._parameters(saved)
        # Discard bridge stderr, which contains generated code and scene text.
        # Never forward raw process errors into a model or the application log.
        with open(os.devnull, "w") as errlog:
            async with asyncio.timeout(300):
                async with stdio_client(parameters, errlog=errlog) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=240,
                                             client_info=types.Implementation(name="video-workbench", version="1")) as session:
                        await session.initialize()
                        rows, cursor, seen = [], None, set()
                        for _ in range(10):
                            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
                            rows.extend(item.model_dump(mode="json", by_alias=True, exclude_none=True,
                                                        exclude_unset=True) for item in page.tools)
                            if len(rows) > 100:
                                raise IntegrationError("The Blender bridge returned more than 100 tools.")
                            cursor = page.next_cursor
                            if not cursor:
                                break
                            if cursor in seen or len(cursor) > 2000:
                                raise IntegrationError("The Blender bridge returned an invalid catalog cursor.")
                            seen.add(cursor)
                        else:
                            raise IntegrationError("The Blender tool catalog exceeded 10 pages.")
                        catalog = []
                        for row in _clean_catalog(rows):
                            if row["name"] in READ_TOOLS | {CODE_TOOL}:
                                is_read = row["name"] in READ_TOOLS
                                catalog.append({**row, "name": PREFIX + row["name"],
                                                "annotations": {"readOnlyHint": is_read},
                                                "dispatch_tool": "integration_call" if is_read else "integration_execute"})
                        if not any(row["name"] == PREFIX + "get_scene_info" for row in catalog):
                            raise IntegrationError("The pinned Blender bridge did not expose scene inspection.")
                        latest = self._current(saved["generation"])
                        if remote_name is None:
                            # A stdio handshake alone is not proof Blender is open.
                            probe = await session.call_tool("get_scene_info", {"limit": 1})
                            result = probe.model_dump(mode="json", by_alias=True, exclude_none=True)
                            _json_size(result, MAX_BYTES, "Blender result")
                            if _tool_error(result):
                                raise IntegrationError("The Blender addon did not answer scene inspection. " + SETUP)
                            return catalog, None
                        row = next((row for row in self._permitted(catalog, latest)
                                    if row["name"] == PREFIX + remote_name), None)
                        if row is None:
                            raise IntegrationError("Enable Blender Python execution in Integrations before using this tool.")
                        if read_only and remote_name not in READ_TOOLS:
                            raise IntegrationError("Blender Python requires integration_execute and explicit host permission.")
                        try:
                            Draft202012Validator(row["inputSchema"]).validate(arguments)
                        except ValidationError:
                            raise IntegrationError("Arguments do not match the Blender tool schema. Read integration_tools first.") from None
                        self._current(saved["generation"])
                        if native_image is not None:
                            # Upstream falls back from an unknown datablock name
                            # to a filesystem path. Confirm the name exists with
                            # this fixed, read-only host script first. repr() keeps
                            # the entire untrusted name inside one Python literal;
                            # this never grants arbitrary agent code execution.
                            code = ("import bpy\nprint('VWB_IMAGE_EXISTS:' + ('yes' if bpy.data.images.get("
                                    + repr(native_image) + ") is not None else 'no'))")
                            probe = await session.call_tool(CODE_TOOL, {"code": code})
                            evidence = probe.model_dump(mode="json", by_alias=True, exclude_none=True)
                            if _tool_error(evidence) or not any(
                                block.get("type") == "text" and "VWB_IMAGE_EXISTS:yes" in block.get("text", "")
                                for block in evidence.get("content", [])
                            ):
                                raise IntegrationError("That image name is not present in Blender. Choose a loaded image or a workspace image path.")
                            self._current(saved["generation"])
                        response = await session.call_tool(remote_name, arguments)
                        self._current(saved["generation"])
                        result = response.model_dump(mode="json", by_alias=True, exclude_none=True)
                        _json_size(result, MAX_BYTES, "Blender result")
                        if _tool_error(result):
                            result["isError"] = True
                        return catalog, result

    def _run(self, saved: dict, **kwargs):
        lock = self.store.root / "operation.lock"
        if lock.is_symlink():
            raise IntegrationError("Blender bridge lock must not be a symlink.")
        try:
            with FileLock(lock, timeout=1):
                return asyncio.run(self._operation(saved, **kwargs))
        except IntegrationError:
            raise
        except Timeout:
            raise IntegrationError("Another Blender call is running. Wait for it to finish.") from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            pending = [exc]
            while pending:
                error = pending.pop()
                if isinstance(error, IntegrationError):
                    raise error from None
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions)
            raise IntegrationError("Blender did not complete the MCP operation. "
                                   "Keep Blender and its addon open, simplify long renders and try again. "
                                   "An interrupted edit may already have changed the scene; inspect it before retrying.") from None

    def connect(self) -> dict:
        with self.store.lock:
            saved = self.store.read()
            saved.update(enabled=True, generation=secrets.token_hex(16))
            self.store.write(saved)
        try:
            tools, _ = self._run(saved)
            with self.store.lock:
                latest = self._current(saved["generation"])
                latest.update(tools=tools, verified_at=time.time())
                latest.pop("error", None)
                self.store.write(latest)
        except IntegrationError as exc:
            with self.store.lock:
                latest = self.store.read()
                if latest.get("generation") == saved["generation"]:
                    latest.pop("verified_at", None)
                    latest["error"] = str(exc)
                    self.store.write(latest)
            raise
        return self.status()

    def refresh(self) -> dict:
        if not self.store.read().get("enabled"):
            raise IntegrationError("Connect Blender MCP in Integrations first.")
        return self.connect()

    def disconnect(self) -> dict:
        with self.store.lock:
            saved = self.store.read()
            saved.update(enabled=False, generation=secrets.token_hex(16))
            saved.pop("verified_at", None)
            saved.pop("error", None)
            self.store.write(saved)
        return self.status()

    def tools(self, query="", limit=30, offset=0) -> dict:
        if (not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 50
                or type(offset) is not int or offset < 0):
            raise IntegrationError("Use a text query, limit 1–50 and a nonnegative offset.")
        saved = self.store.read()
        if not self._prepared() or not saved.get("enabled") or not saved.get("verified_at"):
            raise IntegrationError("Connect Blender MCP in Integrations before using its tools.")
        found = [row for row in self._permitted(saved.get("tools", []), saved)
                 if query.lower() in (row["name"] + " " + row.get("description", "")).lower()]
        return {"integration": "blender_mcp", "tools": found[offset:offset + limit], "total": len(found),
                "offset": offset, "limit": limit, "verified_at": saved["verified_at"],
                "notice": "Live Blender scene; use look after edits. " + TRUST}

    def _workspace_image(self, ws, value) -> Path | None:
        """Pin workspace pixels privately before passing a filename to Blender."""
        if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value:
            raise IntegrationError("Choose a loaded Blender image name or a workspace image path.")
        if not any(char in value for char in ("/", "\\")) and not value.startswith("~") and value not in {".", ".."}:
            return None  # A datablock name, confirmed inside Blender before use.
        if "\\" in value or "://" in value:
            raise IntegrationError("Use a regular image in this workspace, with forward slashes in its path.")
        root = Path(os.path.abspath(ws.work))
        path = Path(value).expanduser()
        if value.startswith("/work/"):
            parts = Path(value.removeprefix("/work/")).parts
        elif path.is_absolute():
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                raise IntegrationError("Blender image paths must stay inside this conversation's work folder.") from None
        else:
            parts = Path(value.removeprefix("work/")).parts
        if not parts or len(parts) > 32 or any(part in {".", ".."} for part in parts):
            raise IntegrationError("Choose a workspace image path without traversal.")
        suffix = Path(parts[-1]).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise IntegrationError("Choose a supported workspace image: PNG, JPEG, WebP, BMP, TIFF, EXR, HDR or TGA.")
        descriptors, snapshot = [], None
        try:
            descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1:
                    flags |= os.O_DIRECTORY
                descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
            if not stat.S_ISREG(os.fstat(descriptors[-1]).st_mode):
                raise IntegrationError("Blender can inspect only regular workspace image files.")
            private = self._directory(self.runtime / "tmp")
            fd, filename = tempfile.mkstemp(prefix="workspace-image-", suffix=suffix, dir=private)
            snapshot = Path(filename)
            total = 0
            with os.fdopen(fd, "wb") as target, os.fdopen(os.dup(descriptors[-1]), "rb") as source:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise IntegrationError("The workspace image exceeds 64 MiB. Inspect a smaller image.")
                    target.write(chunk)
            return snapshot
        except (OSError, IntegrationError) as exc:
            if snapshot:
                snapshot.unlink(missing_ok=True)
            if isinstance(exc, IntegrationError):
                raise
            raise IntegrationError("Could not safely read that workspace image. Symlinks and special files are not supported.") from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def call(self, ws, tool: str, arguments: dict, *, read_only=True) -> dict:
        if not isinstance(tool, str) or not tool.startswith(PREFIX):
            raise IntegrationError("Choose a Blender tool from integration_tools.")
        remote_name = tool.removeprefix(PREFIX)
        if remote_name not in READ_TOOLS | {CODE_TOOL}:
            raise IntegrationError("This Blender preset does not expose cloud asset, paid generation or arbitrary bridge tools.")
        if not isinstance(arguments, dict):
            raise IntegrationError("Blender tool arguments must be an object.")
        _json_size(arguments, 256 * 1024, "Blender arguments")
        saved = self.store.read()
        if not saved.get("enabled") or not saved.get("verified_at"):
            raise IntegrationError("Connect Blender MCP in Integrations before using its tools.")
        if remote_name == CODE_TOOL and (read_only or not saved.get("allow_code_execution")):
            raise IntegrationError("Enable Blender Python in Integrations and use integration_execute for code.")
        snapshot, native_image, dispatched = None, None, arguments
        if remote_name == "look" and arguments.get("image") is not None:
            snapshot = self._workspace_image(ws, arguments["image"])
            if snapshot is not None:
                dispatched = {**arguments, "image": str(snapshot)}
            else:
                native_image = arguments["image"]
        try:
            _, result = self._run(saved, remote_name=remote_name, arguments=dispatched,
                                  read_only=read_only, native_image=native_image)
        finally:
            if snapshot:
                snapshot.unlink(missing_ok=True)
        return import_result(ws, tool, arguments, result, f"blender://127.0.0.1:{saved.get('port', 9876)}",
                             provider="blender_mcp")
