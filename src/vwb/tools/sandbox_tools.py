"""Provider-independent scripts, executed only in a constrained local Docker container.

Agents create temporary or reusable scripts with file_write, then run them here. The
host never evaluates their command, and no gateway credentials or sockets are mounted.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vwb.agents.host_codex import IMAGE
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult, schema

OUTPUT_BYTES = 16_000  # per stream; drain the rest without buffering it
MAX_TIMEOUT = 1800
LOG_BYTES = 8 * 1024 * 1024


class _Capture:
    def __init__(self, stream, log=None):
        self.data = bytearray()
        self.total = 0
        self.thread = threading.Thread(target=self._read, args=(stream, log), daemon=True)
        self.thread.start()

    def _read(self, stream, log):
        try:
            while chunk := getattr(stream, 'read1', stream.read)(8192):
                if log is not None and self.total < LOG_BYTES:
                    log.write(chunk[:LOG_BYTES - self.total])
                    log.flush()
                self.total += len(chunk)
                self.data.extend(chunk[:max(0, OUTPUT_BYTES - len(self.data))])
        finally:
            stream.close()

    def text(self) -> str:
        self.thread.join(timeout=2)
        return bytes(self.data).decode("utf-8", errors="replace")


def _arguments(ctx: ToolContext, args: dict[str, Any]) -> tuple[list[str], str, float]:
    argv = args.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
            or any(not isinstance(value, str) or "\0" in value for value in argv)
            or not argv[0] or sum(len(value) for value in argv) > 32_000):
        raise ValueError("argv must be 1–128 command arguments, at most 32,000 characters, without null bytes")
    timeout = args.get("timeout_s", 60)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 1 <= timeout <= MAX_TIMEOUT):
        raise ValueError(f"timeout_s must be between 1 and {MAX_TIMEOUT} seconds")
    cwd = args.get("cwd", ".")
    if not isinstance(cwd, str) or Path(cwd).is_absolute() or "\0" in cwd:
        raise ValueError("cwd must be relative to work/")
    root = ctx.ws.work.resolve()
    directory = (root / cwd).resolve()
    if directory != root and root not in directory.parents:
        raise ValueError("cwd is outside the request's work folder")
    if not directory.is_dir():
        raise ValueError("cwd does not exist; create it with file_write first")
    return argv, "/workspace/" + directory.relative_to(root).as_posix(), float(timeout)


def _options(args):
    network = args.get('network', 'none')
    if network not in ('none', 'public'):
        raise ValueError('network must be none or public (public HTTP/HTTPS only)')
    if type(args.get('background', False)) is not bool:
        raise ValueError('background must be a boolean')
    if type(args.get('models', False)) is not bool:
        raise ValueError('models must be a boolean')
    memory = args.get('memory_mb', 512)
    cpus = args.get('cpus', 1)
    if type(memory) is not int or not 256 <= memory <= 4096:
        raise ValueError('memory_mb must be an integer from 256 to 4096')
    if type(cpus) is not int or not 1 <= cpus <= 4:
        raise ValueError('cpus must be an integer from 1 to 4')
    return network, memory, cpus


def _command(ctx: ToolContext, argv: list[str], cwd: str, timeout: float, name: str,
             args: dict | None = None) -> list[str]:
    network, memory, cpus = _options(args or {})
    labels = ["--label", "vwb.sandbox=true"]
    job = os.environ.get("VWB_UI_JOB_ID", "")
    if re.fullmatch(r"[a-f0-9]{32}", job):
        labels += ["--label", f"vwb.ui-job={job}"]
    command = ["docker", "run", "--rm", "--init", "--pull=never", "--name", name, *labels,
            "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=64", f"--cpus={cpus}", f"--memory={memory}m", f"--memory-swap={memory}m", "--stop-timeout=1",
            "--user=1001:1001", "--tmpfs=/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--ulimit", "fsize=1073741824:1073741824", "--ulimit", "nofile=256:256",
            "--volume", f"{ctx.ws.work.resolve()}:/workspace:rw", "--workdir", cwd,
            '--env=HOME=/workspace/.sandbox/home', '--env=PYTHONUSERBASE=/workspace/.sandbox/user',
            '--env=PIP_CACHE_DIR=/workspace/.sandbox/cache/pip', '--env=PIP_DISABLE_PIP_VERSION_CHECK=1',
            '--env=PYTHONUNBUFFERED=1', '--env=GIT_TERMINAL_PROMPT=0',
            '--env=PATH=/workspace/.sandbox/user/bin:/usr/local/bin:/usr/bin:/bin']
    if network == 'public' or (args or {}).get('models'):
        from vwb.sandbox import model_client, runner
        command += ['--interactive', '--volume', f'{Path(runner.__file__).resolve()}:/run/vwb-network.py:ro']
        options = ['--public'] if network == 'public' else []
        if (args or {}).get('models'):
            command += ['--volume', f'{Path(model_client.__file__).resolve()}:/run/vwb-sdk/vwb_models.py:ro',
                        '--env=PYTHONPATH=/run/vwb-sdk']
            options.append('--models')
        argv = ['python3', '-I', '/run/vwb-network.py', *options, '--', *argv]
    return command + ["--entrypoint=/usr/bin/timeout", IMAGE, "--signal=KILL", "--", f"{timeout:g}s", *argv]


def _cancelled(ctx: ToolContext) -> bool:
    event = ctx.state.get("cancel_event")
    if event is not None and event.is_set():
        return True
    return bool(ctx.swarm is not None and ctx.swarm.stop.is_set())


def sandbox_run(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    _arguments(ctx, args)
    network, _, _ = _options(args)
    if network == 'public':
        # Go through the ordinary policy/approval/ledger path a second time for
        # network authority. A dynamic flag cannot bypass sandbox.network=ask.
        guarded = Tool('sandbox_run', '', schema({}), _dispatch, 'sandbox.network', 'sandbox')
        return Registry([guarded]).call(ctx, 'sandbox_run', args)
    return _dispatch(ctx, args)


def _dispatch(ctx, args):
    if args.get('models') and not _model_permissions(ctx):
        return ToolResult.error('This agent has no inherited model-tool permissions.')
    if args.get('background'):
        from vwb.sandbox.jobs import launch
        return ToolResult.json(launch(ctx, args))
    return _execute(ctx, args)


MODEL_TOOLS = ('model_providers', 'model_catalog', 'model_request')


def _model_permissions(ctx):
    inherited = ctx.state.get('allowed_tools')
    return [name for name in MODEL_TOOLS if Registry.allowed_for_role(ctx.role, name)
            and (inherited is None or name in inherited)]


class _BridgeCancel:
    def __init__(self, ctx, stop, job):
        self.ctx, self.stop, self.job = ctx, stop, job

    def is_set(self):
        return (self.stop.is_set() or _cancelled(self.ctx)
                or bool(self.job and (self.job['directory'] / 'cancel').exists()))


def _model_handler(ctx, deadline, job=None):
    """The relay receives only this closure, never a registry or credentials."""
    from vwb.tools import build_registry
    registry = build_registry()
    permissions = _model_permissions(ctx)

    def call(name, args, stop):
        state = {**ctx.state, 'allowed_tools': permissions, 'model_deadline': deadline,
                 'cancel_event': _BridgeCancel(ctx, stop, job)}
        bridge = ToolContext(ws=ctx.ws, agent=ctx.agent, role=ctx.role,
                             gateway=ctx.gateway, state=state)
        if time.monotonic() >= deadline:
            return {'is_error': True, 'text': 'Sandbox model request deadline expired.', 'data': None}
        result = registry.call(bridge, name, args)
        return {'is_error': result.is_error, 'text': result.text, 'data': result.data}

    return call


def _execute(ctx: ToolContext, args: dict[str, Any], *, job=None) -> ToolResult:
    argv, cwd, timeout = _arguments(ctx, args)
    network, _, _ = _options(args)
    relay = network == 'public' or args.get('models', False)
    if _cancelled(ctx):
        return ToolResult.error("Sandbox command cancelled before launch")
    if not shutil.which("docker"):
        return ToolResult.error("Docker is unavailable. Start Docker and build docker/codex-agent; no host execution is allowed.")
    name = f"vwb-sandbox-{job['id'] if job else uuid.uuid4().hex}"
    command = _command(ctx, argv, cwd, timeout, name, args)
    started = time.monotonic()
    proc = None
    status = "completed"
    with contextlib.ExitStack() as stack:
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE if relay else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if relay:
                from vwb.sandbox.transport import PublicProcess
                handler = _model_handler(ctx, started + timeout, job) if args.get('models') else None
                proc = PublicProcess(proc, model_handler=handler, allow_public=network == 'public')
            out_log = stack.enter_context((job['directory'] / 'stdout.log').open('wb')) if job else None
            err_log = stack.enter_context((job['directory'] / 'stderr.log').open('wb')) if job else None
            stdout, stderr = _Capture(proc.stdout, out_log), _Capture(proc.stderr, err_log)
            heartbeat = 0
            while proc.poll() is None:
                if _cancelled(ctx) or (job and (job['directory'] / 'cancel').exists()):
                    status = "cancelled"
                    break
                if job and time.monotonic() - heartbeat > 2:
                    from vwb.sandbox.jobs import update
                    update(job['directory'], status='running', heartbeat=time.time())
                    heartbeat = time.monotonic()
                if time.monotonic() - started > timeout + 5:  # includes a bounded startup grace
                    status = "timed_out"
                    break
                time.sleep(0.05)
            if status != "completed":
                _remove(name)
                if proc.poll() is None:
                    proc.kill()
            proc.wait(timeout=5)
            if proc.returncode in (124, 137) and status == "completed":
                status = "timed_out" if time.monotonic() - started >= timeout else "terminated"
            elif proc.returncode != 0 and status == "completed":
                status = "failed"
            out, err = stdout.text(), stderr.text()
            data = {"status": status, "exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 3),
                    "stdout": out, "stderr": err, "stdout_truncated": stdout.total > OUTPUT_BYTES,
                    "stderr_truncated": stderr.total > OUTPUT_BYTES,
                    "stdout_bytes": stdout.total, "stderr_bytes": stderr.total,
                    "log_limit_bytes": LOG_BYTES if job else 0,
                    "logs_truncated": bool(job and max(stdout.total, stderr.total) > LOG_BYTES),
                    "cwd": cwd, "network": 'public_http_https' if network == 'public' else 'disabled',
                    "models": bool(args.get('models')),
                    "filesystem": "Only the request work folder is writable and shared",
                    "note": "Environments and package caches persist under work/.sandbox. Root is read-only; "
                            "vendor keys and host sockets are absent. Public mode supports proxy-aware HTTP(S) clients, "
                            "pip and Git HTTPS; private networks and direct sockets are unavailable."}
            result = ToolResult.json(data)
            result.is_error = status != "completed"
            return result
        finally:
            _remove(name)
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

def sandbox_environment(ctx, args):
    name = args.get('name', 'default')
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', name):
        raise ValueError('Environment name must be 1–48 letters, digits, underscores or hyphens')
    packages = args.get('packages', [])
    if (not isinstance(packages, list) or len(packages) > 100 or any(
            not isinstance(value, str) or not value or value.startswith('-') or '\0' in value
            or len(value) > 2000 for value in packages)):
        raise ValueError('packages must contain at most 100 package requirements or public URLs')
    path = f'/workspace/.sandbox/envs/{name}'
    script = '''import fcntl,json,pathlib,subprocess,sys
p=pathlib.Path(sys.argv[1])
p.parent.mkdir(parents=True,exist_ok=True)
lock=(p.parent/('.'+p.name+'.lock')).open('a')
try:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:
    print('This environment is busy. Wait for its current installation or choose another name.',file=sys.stderr)
    raise SystemExit(2)
subprocess.run([sys.executable,'-m','venv',str(p)],check=True)
python=str(p/'bin/python'); packages=json.loads(sys.argv[2])
if packages:
    subprocess.run([python,'-m','pip','install','--',*packages],check=True)
result=subprocess.run([python,'-m','pip','freeze'],capture_output=True,text=True,check=True)
(p/'requirements.freeze.txt').write_text(result.stdout)
print(json.dumps({'python':python,'requirements':str(p/'requirements.freeze.txt')}))
'''
    execution = {key: args[key] for key in ('timeout_s', 'background', 'memory_mb', 'cpus', 'network', 'models') if key in args}
    execution.setdefault('network', 'public' if packages else 'none')
    execution.setdefault('timeout_s', 600)
    execution['argv'] = ['python3', '-c', script, path, json.dumps(packages)]
    # Execution and network policies both apply to this convenience tool.
    result = Registry(TOOLS).call(ctx, 'sandbox_run', execution)
    if not result.is_error and isinstance(result.data, dict) and not result.data.get('parked'):
        result.data.update(environment=name, python=path + '/bin/python',
                           requirements=path + '/requirements.freeze.txt')
        result.text = json.dumps(result.data)
    return result


def sandbox_status(ctx, args):
    from vwb.sandbox.jobs import status
    return ToolResult.json(status(ctx, args))


def _remove(name: str) -> None:
    try:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the in-container timeout also bounds normal orphaned commands


EXECUTION = {'network': {'type': 'string', 'enum': ['none', 'public']},
             'models': {'type': 'boolean'},
             'background': {'type': 'boolean'}, 'memory_mb': {'type': 'integer', 'minimum': 256, 'maximum': 4096},
             'cpus': {'type': 'integer', 'minimum': 1, 'maximum': 4},
             'timeout_s': {'type': 'number', 'minimum': 1, 'maximum': MAX_TIMEOUT}}

TOOLS = [Tool("sandbox_run", "Run an agent-written script or command inside a resource-limited Docker sandbox. "
              "First use file_write to create a reusable or temporary script in work/. argv is an argument list, "
              "e.g. ['python3','scripts/build.py']; use ['bash','-lc','...'] only when shell syntax is needed. "
              "Only work/ is shared; vendor credentials are absent. network=public enables public HTTP(S) "
              "through a proxy for pip/curl/Git HTTPS, with private destinations blocked. Default network=none. "
              "models=true enables a scoped model gateway, even with network=none: Python scripts can import "
              "providers, catalog, request from vwb_models (stdlib only, also inside venvs). Provider keys stay "
              "on the host; calls keep this agent's role, policy and inherited model-tool permissions. "
              "Create reusable Python environments with sandbox_environment, or pip --target for loose packages. "
              "background=true returns a durable job ID; poll or cancel with sandbox_status. Max runtime 30 minutes. "
              "Use checkpoints for larger batches. Requires Docker and the local Codex image; no host fallback.",
              schema({"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
                      "cwd": {"type": "string"}, **EXECUTION}, ["argv"]),
              sandbox_run, "sandbox.run", "sandbox"),
         Tool('sandbox_environment', 'Create/reuse a request-local Python venv and optionally install pip requirements '
              'or HTTPS/Git HTTPS URLs. Saves pip freeze alongside the environment. Returns the Python path for '
              'later sandbox_run calls. Installation defaults to public network when packages are supplied. '
              'Use background=true for slow builds. No host or system Python changes.',
              schema({'name': {'type': 'string'}, 'packages': {'type': 'array', 'items': {'type': 'string'}},
                      **EXECUTION}), sandbox_environment, 'sandbox.environment', 'sandbox'),
         Tool('sandbox_status', 'List recent sandbox jobs, or inspect/poll/cancel a job by ID. Output is paged with '
              'stdout_offset and stderr_offset byte positions; pass next offsets for subsequent output. '
              'Jobs survive chat turns and have a hard 30-minute deadline. Stopped supervisors never replay commands.',
              schema({'job_id': {'type': 'string'}, 'cancel': {'type': 'boolean'},
                      'wait_s': {'type': 'number', 'minimum': 0, 'maximum': 30},
                      'stdout_offset': {'type': 'integer', 'minimum': 0},
                      'stderr_offset': {'type': 'integer', 'minimum': 0}}), sandbox_status, 'sandbox.status', 'sandbox')]
