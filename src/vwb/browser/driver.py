"""Async subprocess adapter for agent-browser 0.39.1; no agent loop or UI policy.

The service serializes actions and owns the user/agent control lease. This module
never forwards provider credentials or reads a user's agent-browser config.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import socket
import stat
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

from . import runtime

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
# These are environment plumbing, never credentials or browser launch overrides.
ENV_KEYS = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"})
ADMIN_COMMANDS = frozenset({"install", "auth", "profiles", "profile", "state", "session", "connect", "dashboard", "batch", "daemon", "inspect", "extension", "plugin", "doctor"})


def _validate_operation_args(args: Sequence[str]) -> None:
    # The native parser scans ALL argv, including typed text, and has no `--`
    # terminator. A global-flag blocklist misses new options. Permit only the
    # operation flags we deliberately emit, in their actual option positions.
    allowed_positions: set[int] = set()
    if args[0] == "snapshot":
        i = 1
        while i < len(args):
            if args[i] in {"-i", "-c"}:
                allowed_positions.add(i)
                i += 1
            elif args[i] in {"-d", "-s"}:
                allowed_positions.add(i)
                i += 2  # The following operand is never a permitted flag.
            else:
                i += 1
    elif args[0] == "screenshot":
        allowed_positions = {i for i in range(2, len(args)) if args[i] in {"--annotate", "--full"}}
    elif args[0] == "scroll" and len(args) > 3 and args[3] == "--selector":
        allowed_positions.add(3)
    for i, value in enumerate(args):
        if (value.startswith("--") or re.match(r"^-[A-Za-z?]", value)) and i not in allowed_positions:
            raise ValueError("Browser operation arguments cannot override private runtime settings with flag-shaped text")


class BrowserDriverError(RuntimeError):
    def __init__(self, message: str, *, code: str = "browser_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    stdout: bytes
    stderr: bytes = b""


@dataclass(frozen=True)
class ScreenshotResult:
    data: bytes
    mime_type: str
    annotations: list[dict[str, Any]]
    width: int
    height: int


Runner = Callable[..., Awaitable[ProcessOutput]]


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    parts, total = [], 0
    while chunk := await stream.read(65536):
        total += len(chunk)
        if total > MAX_OUTPUT_BYTES:
            raise BrowserDriverError("Browser response exceeded the output limit", code="output_limit")
        parts.append(chunk)
    return b"".join(parts)


async def subprocess_runner(argv: Sequence[str], *, env: dict[str, str], cwd: Path, timeout_s: float) -> ProcessOutput:
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    tasks = [asyncio.create_task(_read_bounded(proc.stdout)), asyncio.create_task(_read_bounded(proc.stderr))]
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await asyncio.gather(*tasks)
            return ProcessOutput(await proc.wait(), stdout, stderr)
    except BaseException:
        # Reap this CLI only. Its daemon may have applied the action; never
        # automatically retry. The service decides whether to close the session.
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class AgentBrowserDriver:
    def __init__(
        self, private_dir: Path, session_name: str, *, binary: Path | None = None,
        timeout_s: float = 30, remote_cdp: str | None = None, runner: Runner | None = None,
        executable_path: Path | None = None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session_name):
            raise ValueError("Browser session names need 1–40 letters, numbers, hyphens or underscores")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise ValueError("Browser timeout must be between 0 and 120 seconds")
        self.private_dir = runtime.private_directory(Path(private_dir))
        self.session_name = session_name
        self.socket_dir = runtime.socket_directory(self.private_dir, session_name)
        self.config_path = self.private_dir / "config.json"
        self.binary = Path(binary).resolve() if binary is not None else None
        self.timeout_s = timeout_s
        self.remote_cdp = remote_cdp if remote_cdp is not None else os.environ.get("VWB_BROWSER_CDP_URL")
        self.executable_path = executable_path
        self.runner = runner or subprocess_runner
        self.mode: str | None = None
        self._binary_verified: Path | None = None

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ENV_KEYS}
        env.update({"AGENT_BROWSER_SOCKET_DIR": str(self.socket_dir), "AGENT_BROWSER_DEFAULT_TIMEOUT": "25000"})
        if self.mode == "remote" and self.remote_cdp:
            # Remote URLs can contain authentication. Keep them out of argv,
            # request config, logs and any returned errors/data.
            env["AGENT_BROWSER_CDP"] = self.remote_cdp
        return env

    def _redact_text(self, value: str) -> str:
        if self.remote_cdp:
            value = value.replace(self.remote_cdp, "[remote browser endpoint]")
            parsed = urlsplit(self.remote_cdp)
            for secret in (parsed.password, parsed.query):
                if secret:
                    value = value.replace(secret, "[redacted]")
            for pair in parsed.query.split("&"):
                if "=" in pair and pair.split("=", 1)[1]:
                    value = value.replace(pair.split("=", 1)[1], "[redacted]")
        # Do not leak credentials from error URLs or common credential fields.
        value = re.sub(r"(?i)(authorization\s*[:=]\s*|bearer\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;\"']+", r"\1[redacted]", value)
        value = re.sub(r"(https?|wss?)://[^\s/@:]+:[^\s/@]+@", r"\1://[redacted]@", value)
        return value

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        if isinstance(value, dict):
            return {k: "[redacted]" if re.fullmatch(r"(?i)(authorization|api[_-]?key|token|password|secret|cdpUrl|webSocketDebuggerUrl)", k) else self._redact(v) for k, v in value.items()}
        return value

    def _binary(self) -> Path:
        if self._binary_verified is None:
            try:
                self._binary_verified = self.binary or runtime.binary_path()
            except (OSError, RuntimeError) as exc:
                raise BrowserDriverError(str(exc), code="not_installed") from None
        return self._binary_verified

    async def run(self, args: Sequence[str], *, timeout_s: float | None = None) -> dict[str, Any]:
        if not args or not all(isinstance(a, str) and "\0" not in a for a in args):
            raise ValueError("Browser operation arguments must be nonempty text arguments")
        if args[0].startswith("-") or args[0] in ADMIN_COMMANDS:
            raise ValueError("Browser administrative commands are not operation arguments")
        _validate_operation_args(args)
        if not self.config_path.is_file() or self.config_path.is_symlink():
            raise BrowserDriverError("Start the browser session before sending operations", code="not_started")
        duration = self.timeout_s if timeout_s is None else timeout_s
        if not math.isfinite(duration) or not 0 < duration <= 120:
            raise ValueError("Browser timeout must be between 0 and 120 seconds")
        argv = [str(self._binary()), "--config", str(self.config_path), "--session", self.session_name, "--json", *args]
        try:
            result = await self.runner(argv, env=self._env(), cwd=self.private_dir, timeout_s=duration)
        except TimeoutError:
            raise BrowserDriverError("Browser command timed out; its effect is unknown. Observe before retrying.", code="timeout") from None
        except OSError as exc:
            raise BrowserDriverError(f"Could not run the browser: {self._redact_text(str(exc))[:1000]}", code="process_error") from None
        try:
            response = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            detail = self._redact_text(result.stderr.decode("utf-8", errors="replace"))[:1000]
            raise BrowserDriverError(f"Browser returned invalid JSON{': ' + detail if detail else ''}", code="invalid_response") from None
        if not isinstance(response, dict):
            raise BrowserDriverError("Browser returned an invalid response envelope", code="invalid_response")
        if result.returncode != 0 or response.get("success") is not True:
            detail = str(response.get("error") or result.stderr.decode("utf-8", errors="replace") or "Browser command failed")
            raise BrowserDriverError(self._redact_text(detail)[:1500], code=str(response.get("code") or "command_failed"))
        data = response.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise BrowserDriverError("Browser returned invalid operation data", code="invalid_response")
        return self._redact(data)

    async def start(self, mode: str = "headed", viewport: tuple[int, int] = (1280, 800)) -> dict:
        if mode not in {"headed", "headless", "remote"}:
            raise ValueError("Browser mode must be headed, headless or remote")
        if len(viewport) != 2 or any(type(v) is not int or not 320 <= v <= 4096 for v in viewport):
            raise ValueError("Browser viewport dimensions must be integers from 320 to 4096")
        if mode == "remote":
            if not self.remote_cdp:
                raise BrowserDriverError("Set VWB_BROWSER_CDP_URL on the host to attach a remote browser", code="remote_unconfigured")
            try:
                parsed = urlsplit(self.remote_cdp)
            except ValueError:
                raise BrowserDriverError("The host remote browser endpoint is invalid", code="remote_unconfigured") from None
            if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
                raise BrowserDriverError("The host remote browser endpoint must use http(s) or ws(s)", code="remote_unconfigured")
        self._binary()  # No config changes or launch until installation is verified.
        self.mode = mode
        config: dict[str, Any] = {
            "json": True, "headed": mode == "headed", "engine": "chrome", "pinTab": True,
            "autoConnect": False, "allowFileAccess": False, "ignoreHttpsErrors": False,
            "contentBoundaries": False, "hideScrollbars": False, "noAutoDialog": True,
            "downloadPath": str(runtime.private_directory(self.private_dir / "downloads")),
            "screenshotDir": str(runtime.private_directory(self.private_dir / "screenshots")),
        }
        if mode != "remote":
            config["profile"] = str(runtime.private_directory(self.private_dir / "profile"))
            if self.executable_path is not None:
                config["executablePath"] = str(self.executable_path.resolve())
        temp = self.config_path.with_name(f"config-{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(config), encoding="utf-8")
            temp.chmod(0o600)
            os.replace(temp, self.config_path)
        finally:
            temp.unlink(missing_ok=True)
        # Remote sessions get a fresh owned tab, leaving the user's existing
        # current page untouched. close() later disconnects the remote browser.
        opened = await self.run(["tab", "new", "about:blank"] if mode == "remote" else ["open", "about:blank"])
        await self.run(["set", "viewport", str(viewport[0]), str(viewport[1])])
        return {"mode": mode, "viewport": {"width": viewport[0], "height": viewport[1]}, **opened}

    def alive(self) -> bool:
        """Best-effort owned daemon identity check; no CLI or browser launch."""
        pid_path = self.socket_dir / f"{self.session_name}.pid"
        endpoint = self.socket_dir / f"{self.session_name}.sock"
        try:
            if pid_path.is_symlink() or endpoint.is_symlink() or not stat.S_ISSOCK(endpoint.stat().st_mode):
                return False
            pid = int(pid_path.read_text().strip())
            if pid <= 1 or pid_path.stat().st_uid != os.getuid() or endpoint.stat().st_uid != os.getuid():
                return False
            os.kill(pid, 0)
            command = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=1, check=False).stdout.strip()
            expected = str(self.binary or runtime.runtime_dir() / "agent-browser")
            if command != expected:
                return False
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.25)
                connection.connect(str(endpoint))
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    async def stop(self) -> dict:
        # A GET/stop on an absent session must not create a daemon to close it.
        if not self.alive():
            return {"closed": True, "was_running": False}
        result = await self.run(["close"])
        # The native CLI acknowledges close before its daemon removes the socket.
        # Finish draining that owned daemon so an immediate reopen cannot reuse a
        # process that is still shutting down. Remote close only disconnects CDP.
        for _ in range(50):
            if not await asyncio.to_thread(self.alive):
                return result
            await asyncio.sleep(0.1)
        raise BrowserDriverError("Browser close was acknowledged, but its daemon has not exited. Do not reopen yet.", code="close_pending")

    async def stream_port(self) -> int | None:
        try:
            path = self.socket_dir / f"{self.session_name}.stream"
            if path.is_symlink():
                return None
            port = int(path.read_text().strip())
            return port if 1 <= port <= 65535 else None
        except (OSError, ValueError):
            return None

    async def stream_url(self) -> str | None:
        port = await self.stream_port()
        return f"ws://127.0.0.1:{port}/?pacing=ack&maxFps=15" if port else None

    async def tabs(self) -> list[dict[str, Any]]:
        data = await self.run(["tab", "list"])
        tabs = data.get("tabs")
        if not isinstance(tabs, list) or any(not isinstance(tab, dict) or not isinstance(tab.get("tabId"), str) for tab in tabs):
            raise BrowserDriverError("Browser returned an invalid tab list", code="invalid_response")
        return [{"id": tab["tabId"], **{k: tab.get(k) for k in ("tabId", "targetId", "label", "title", "url", "type", "active")}} for tab in tabs]

    async def snapshot(self, *, interactive: bool = False, compact: bool = False, depth: int | None = None, selector: str | None = None) -> dict:
        args = ["snapshot"]
        if interactive:
            args.append("-i")
        if compact:
            args.append("-c")
        if depth is not None:
            if type(depth) is not int or not 1 <= depth <= 100:
                raise ValueError("Snapshot depth must be from 1 to 100")
            args += ["-d", str(depth)]
        if selector:
            args += ["-s", selector]
        return await self.run(args)

    async def screenshot(self, *, annotate: bool = False, full_page: bool = False) -> ScreenshotResult:
        folder = runtime.private_directory(self.private_dir / "screenshots")
        path = folder / f"capture-{uuid.uuid4().hex}.png"
        args = ["screenshot", str(path)]
        if annotate:
            args.append("--annotate")
        if full_page:
            args.append("--full")
        try:
            result = await self.run(args)
            # Read only the path we supplied, never a path returned by a page or
            # executable. This also keeps artifacts outside the agent mount.
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SCREENSHOT_BYTES:
                raise BrowserDriverError("Browser did not produce a valid bounded screenshot", code="invalid_screenshot")
            data = path.read_bytes()
            try:
                with Image.open(io.BytesIO(data)) as picture:
                    if picture.format != "PNG":
                        raise ValueError("Expected PNG")
                    width, height = picture.size
                    picture.verify()
            except (OSError, ValueError, Image.DecompressionBombError):
                raise BrowserDriverError("Browser screenshot was not a valid PNG", code="invalid_screenshot") from None
            annotations = result.get("annotations", [])
            if not isinstance(annotations, list):
                annotations = []
            return ScreenshotResult(data, "image/png", annotations, width, height)
        finally:
            path.unlink(missing_ok=True)

    async def has_frames(self) -> bool:
        """Conservative top-level frame presence check for cursor placement.

        Native eval runs in the active tab main document even when snapshot
        scope is an iframe. Unknown results mean coordinates are not trusted.
        """
        result = await self.run(["eval", "(() => window.frames.length > 0 || document.querySelector(\"iframe,frame\") !== null)()"] )
        value = result.get("result")
        return value if type(value) is bool else True

    async def bbox(self, ref: str) -> dict:
        result = await self.run(["get", "box", ref])
        if any(not isinstance(result.get(k), (int, float)) or not math.isfinite(result[k]) for k in ("x", "y", "width", "height")):
            raise BrowserDriverError("Browser returned an invalid element box", code="invalid_response")
        # Upstream exposes frame-local coordinates without the frame identity.
        # Do not silently draw these as top-level viewport coordinates.
        return {**result, "coordinate_space": "element-frame", "reliable_for_viewport": False}
