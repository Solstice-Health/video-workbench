"""The `vwb` command line: the chat desk for v0 (spec D24) and operator commands.

Human events (answers, acknowledgements, waivers, accepts) are minted here and signed with
the control plane's key. No agent tool can mint them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from vwb.util.log import console

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Video Workbench: a chat-based video editor built as a general agent harness.")
mcp_app = typer.Typer(help="Serve the toolkit over MCP.")
app.add_typer(mcp_app, name="mcp")


def _ws(request: Optional[str]):
    from vwb.workspace import Workspace
    return Workspace.open(request) if request else Workspace.latest()


@app.command()
def ui(port: int = typer.Option(8765, min=1, max=65535, help="Local browser port."),
       open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the workbench in your browser.")):
    """Open the local chat workbench with live activity, queued messages and video previews."""
    from vwb.web.server import serve
    serve(port=port, open_browser=open_browser)


@app.command()
def chat(message: Optional[str] = typer.Argument(None, help="What you want, in plain words."),
         attach: list[Path] = typer.Option(None, "--attach", "-a", help="Files to attach: videos, documents, images or other files."),
         request: Optional[str] = typer.Option(None, "--request", "-r", help="Continue an existing request."),
         interactive: bool = typer.Option(False, "--interactive", "-i", help="Keep chatting: one conversation, many turns."),
         skip_index: bool = typer.Option(False, help="Do not (re)build the index first.")):
    """Chat with the main agent. Each request is one continuous conversation: a new message continues it."""
    from vwb.orchestrator import chat as run_chat
    if message is None and not interactive:
        raise typer.BadParameter("give a message, or use --interactive")
    while True:
        if message is None:
            try:
                line = console.input("[bold green]you>[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if line in ("/exit", "/quit"):
                break
            if not line:
                continue
            message = line
        try:
            res = run_chat(message, attach=list(attach or []), request_id=request, skip_index=skip_index)
        except KeyboardInterrupt:                  # Ctrl-C stops the turn, not the conversation
            if not interactive:
                raise
            console.print("[yellow]interrupted: the turn stopped; tool calls it finished stay in effect[/]")
            if request is None:                    # the first message had already opened its request
                from vwb.workspace import Workspace
                latest = Workspace.latest()
                request = latest.id if latest.load().message == message else None
            attach, message, skip_index = [], None, request is not None
            continue
        console.rule("main agent")
        console.print(res["reply"])
        if res["outputs"]:
            _print_outputs(res["outputs"])
        console.print(f"\nReview page: [bold]{res['review_page']}[/]")
        if not interactive:
            break
        request, attach, message, skip_index = res["request"], [], None, True
        console.print(f"[dim]request {request}: type the next message, or /exit[/]")


def _print_outputs(rows):
    t = Table(title="outputs")
    for col in ("output", "version", "triage"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["output"], str(r["version"]), r["triage"])
    console.print(t)


@app.command()
def index(request: Optional[str] = typer.Argument(None), force: bool = False):
    """Build or refresh the edit index of a request."""
    from vwb.index.build import build_index
    ws = _ws(request)
    console.print(build_index(ws, force=force, progress=lambda m: console.print(f"  [dim]{m}[/]")))


@app.command()
def status(request: Optional[str] = typer.Argument(None)):
    """Outputs, triage and open questions of a request."""
    from vwb.ask.broker import Broker
    ws = _ws(request)
    req = ws.load()
    console.print(f"[bold]{req.id}[/]: {req.message}")
    _print_outputs(_summary(ws))
    for t in Broker(ws).tickets():
        console.print(f"question {t['id']} [{t['status']}]: {t['question']} (recommended {t['recommended']})")


def _summary(ws):
    from vwb.canvas.store import CanvasStore
    from vwb.util.jsonio import read_json
    store = CanvasStore(ws.canvas)
    return [{"output": o, "version": store.head(o),
             "triage": read_json(ws.qc / o / f"v{store.head(o)}" / "verification.json", {}).get("triage", "NOT SUBMITTED")}
            for o in store.outputs()]


@app.command()
def flags(request: Optional[str] = typer.Argument(None), output: Optional[str] = None, all: bool = False):
    """Flags of a request (open by default)."""
    from vwb.workspace import flags as fl
    from vwb.workspace.ledger import Ledger
    ws = _ws(request)
    rows = fl.current_flags(Ledger(ws.ledger / "ledger.jsonl"), output=output)
    t = Table()
    for col in ("id", "sev", "kind", "output", "status", "summary"):
        t.add_column(col)
    for f in sorted(rows, key=lambda f: (f.get("output") or "", -fl.LEVELS.index(f["severity"]))):
        if all or f["status"] == "open":
            t.add_row(f["id"], f["severity"], f["kind"], str(f.get("output") or "-"), f["status"], f["summary"][:90])
    console.print(t)


@app.command()
def answer(request: str, ticket: str, option: str, text: str = "", by: str = typer.Option("operator")):
    """Answer a question (a signed human event)."""
    from vwb.ask.broker import Broker
    t = Broker(_ws(request)).answer(ticket, option, text=text, by=by)
    console.print(f"answered {t['id']}: {option}")


@app.command()
def ack(request: str, flag_id: str, by: str = typer.Option("operator")):
    """Acknowledge a WARN or HOLD flag by id (a signed human event)."""
    _human_flag_event(request, flag_id, "human.ack", by)


@app.command()
def waive(request: str, flag_id: str, reason: str, by: str = typer.Option("operator"),
          second: Optional[str] = typer.Option(None, help="Second named person, required for BLOCK.")):
    """Waive a flag with a reason. BLOCK needs two named people; some kinds can never be waived."""
    from vwb.config import load_config
    from vwb.workspace import flags as fl
    from vwb.workspace.ledger import Ledger
    ws = _ws(request)
    f = next((x for x in fl.current_flags(Ledger(ws.ledger / "ledger.jsonl")) if x["id"] == flag_id), None)
    if f is None:
        raise typer.BadParameter(f"no flag {flag_id}")
    if fl.base_kind(f["kind"]) in load_config("flag_kinds")["never_waivable"]:
        raise typer.BadParameter(f"{f['kind']} can never be waived")
    if f["severity"] == "BLOCK" and not second:
        raise typer.BadParameter("a BLOCK waiver needs a senior pair: pass --second <name>")
    _human_flag_event(request, flag_id, "human.waive", by, {"reason": reason, "second": second})


def _human_flag_event(request: str, flag_id: str, kind: str, by: str, extra: dict | None = None):
    from vwb.workspace.ledger import Ledger
    ws = _ws(request)
    Ledger(ws.ledger / "ledger.jsonl").append(kind, f"human:{by}", {"id": flag_id, **(extra or {})}, signed=True)
    console.print(f"{kind} {flag_id} by {by}")


@app.command()
def accept(request: str, output: str, by: str = typer.Option(..., help="Your name; acceptance is signed.")):
    """Accept an output's head version. Every WARN-or-higher flag must be acknowledged or waived first."""
    from vwb.canvas.store import CanvasStore
    from vwb.util.jsonio import read_json, write_json
    from vwb.workspace import flags as fl
    from vwb.workspace.ledger import Ledger, sign
    import shutil
    ws = _ws(request)
    v = CanvasStore(ws.canvas).head(output)
    ledger = Ledger(ws.ledger / "ledger.jsonl")
    blocking = [f for f in fl.current_flags(ledger, output=output, version=v)
                if f["status"] == "open" and f["severity"] in ("WARN", "HOLD", "BLOCK")]
    if blocking:
        console.print(f"[red]Cannot accept {output} v{v}: {len(blocking)} open flag(s) need an ack or waiver:[/]")
        for f in blocking:
            console.print(f"  {f['id']} {f['severity']} {f['kind']}: {f['summary'][:100]}")
        raise typer.Exit(1)
    manifest = read_json(ws.qc / output / f"v{v}" / "manifest.json")
    if not manifest:
        raise typer.BadParameter("no verified review package for this version")
    ev = ledger.append("human.accept", f"human:{by}", {"manifest": manifest["signature"], "sha256": manifest["files"]["video"]["sha256"]},
                       output=output, version=v, signed=True)
    rel = ws.release / output / f"v{v}"
    rel.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ws.root / manifest["files"]["video"]["path"], rel)
    release = {"output": output, "version": v, "accepted_by": by, "accept_event": ev["hash"], "manifest": manifest}
    release["signature"] = sign(release)
    write_json(rel / "release.json", release)
    console.print(f"[green]accepted {output} v{v}[/] -> {rel}")


@app.command()
def render(request: str, output: str, version: Optional[int] = None, force: bool = False):
    """Render an output version."""
    from vwb.render.pipeline import render as do_render
    console.print(do_render(_ws(request), output, version, actor="operator", force=force))


@app.command()
def verify(request: str, output: str, version: Optional[int] = None):
    """Verify a rendered output version and rebuild its review package."""
    from vwb.verify.verifier import verify as do_verify
    r = do_verify(_ws(request), output, version, actor="verifier")
    console.print(f"{output} v{r['version']}: {r['triage']}")


@app.command()
def review(request: Optional[str] = typer.Argument(None)):
    """Rebuild the review page."""
    from vwb.review.page import build
    console.print(build(_ws(request)))


@app.command()
def spend(request: Optional[str] = typer.Argument(None)):
    """Model usage of a request per vendor and model (calls, tokens), from the call log."""
    from vwb.workspace.spend import summary
    ws = _ws(request)
    t = Table(title=f"usage: {ws.id}")
    for col in ("provider", "model", "calls", "failed", "input tok", "output tok", "reasoning tok", "cached tok", "seconds"):
        t.add_column(col)
    for r in summary(ws):
        t.add_row(r["provider"], r["model"], str(r["calls"]), str(r["failed"]), f"{r['input']:,}", f"{r['output']:,}",
                  f"{r['reasoning']:,}", f"{r['cached']:,}", f"{r['seconds']:,.0f}")
    console.print(t)


@app.command()
def tools():
    """List the toolkit (name, area, policy capability, current dial mode)."""
    from vwb.tools import build_registry
    r = build_registry()
    t = Table()
    for col in ("tool", "area", "capability", "mode"):
        t.add_column(col)
    for tool in sorted(r.tools.values(), key=lambda x: (x.area, x.name)):
        t.add_row(tool.name, tool.area, tool.capability, r.mode(tool.capability))
    console.print(t)


@app.command()
def doctor(ui_only: bool = False):
    """Check local readiness; --ui-only requires just the Python backend and bundled UI."""
    import importlib.util
    import json
    import os
    import platform
    import re
    import shutil
    import subprocess
    import sys

    from vwb.agents.host_codex import IMAGE
    from vwb.browser.runtime import status as browser_status
    from vwb.config import find_env_file, secrets, slot
    from vwb.render.plates import REMOTION_DIR

    failed = False

    def check(label: str, ok: bool, hint: str = "", *, required: bool = True):
        nonlocal failed
        failed |= required and not ok
        state = "[green]ok[/]" if ok else "[red]missing[/]" if required else "[yellow]optional[/]"
        console.print(f"{state}  {label}", markup=True, highlight=False)
        if not ok and hint:
            console.print(f"    {hint}", markup=False, highlight=False)

    def probe(command: list[str], *, cwd: Path | None = None) -> str | None:
        try:
            options = {"cwd": cwd} if cwd is not None else {}
            result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False, **options)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    check(f"Python {platform.python_version()}", (3, 12) <= sys.version_info[:2] < (3, 14),
          "Use Python 3.12 or 3.13 and run uv sync --frozen.")
    static = Path(__file__).parent / "web" / "static"
    check("Bundled workbench UI", all((static / name).is_file() for name in (
        "index.html", "app.js", "app.css", "vendor/react.production.min.js",
        "vendor/react-dom.production.min.js", "vendor/eeva/components/bundle.js",
    )), "Restore the repository's src/vwb/web/static files and run uv sync --frozen.")
    for binary in ("ffmpeg", "ffprobe"):
        check(binary, bool(shutil.which(binary)), "Install FFmpeg and ensure both ffmpeg and ffprobe are on PATH.",
              required=not ui_only)
    node = shutil.which("node")
    node_version = probe([node, "--version"]) if node else None
    match = re.fullmatch(r"v?(\d+)\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", node_version or "")
    # The pinned Remotion 4 renderer requires Node >=16; setup recommends Node 22.
    check(f"Node.js{f' {node_version}' if match else ''}", bool(match and int(match[1]) >= 16),
          "Install Node.js 22 LTS (the pinned renderer requires at least Node 16).", required=not ui_only)
    npm = shutil.which("npm")
    npm_version = probe([npm, "--version"]) if npm else None
    valid_npm = bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", npm_version or ""))
    check(f"npm{f' {npm_version}' if valid_npm else ''}", valid_npm, "Install npm with Node.js.", required=not ui_only)
    check("Remotion renderer dependencies", (REMOTION_DIR / "node_modules" / "remotion" / "package.json").is_file(),
          "Run npm ci in generators/remotion.", required=not ui_only)
    renderer = REMOTION_DIR / "node_modules" / "@remotion" / "renderer"
    # ensureBrowser's callback runs before any download. Refuse it here: doctor
    # checks the pinned cached browser without installing or launching anything.
    browser_probe = ("require(process.argv[1]).ensureBrowser({logLevel:'silent',"
                     "onBrowserDownload:()=>{throw Error('Not installed')}})"
                     ".then(()=>console.log('ready')).catch(()=>{process.exitCode=1})")
    render_browser_ready = bool(node and renderer.is_dir() and probe(
        [node, "--eval", browser_probe, str(renderer)], cwd=REMOTION_DIR))
    check("Remotion render browser", render_browser_ready,
          "Run npx remotion browser ensure in generators/remotion.", required=not ui_only)
    try:
        font_dir = REMOTION_DIR / "public" / "fonts"
        manifest = json.loads((font_dir / "manifest.json").read_text())
        fonts_ready = bool(manifest) and all(
            row.get("faces") and all((font_dir / face["file"]).is_file() for face in row["faces"])
            for row in manifest
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        fonts_ready = False
    check("Pinned renderer fonts", fonts_ready, "Run npm run setup-fonts in generators/remotion.", required=not ui_only)

    env_file = find_env_file()
    check("Environment file", bool(env_file and env_file.is_file()),
          "Optional when keys are supplied through environment variables or Workbench. Copy .env.example if needed.",
          required=False)
    provider_keys = secrets()
    for provider, key in (("OpenAI", provider_keys.openai), ("Anthropic", provider_keys.anthropic),
                          ("Google", provider_keys.google), ("OpenRouter", provider_keys.openrouter)):
        check(f"{provider} API key configured", bool(key),
              "Configure this provider in .env or Models and APIs before using its models.", required=False)

    docker = shutil.which("docker")
    daemon_ready = bool(docker and probe([docker, "info", "--format", "{{.ServerVersion}}"]))
    check("Docker daemon (Codex agents and sandbox tools)", daemon_ready,
          "Install/start Docker to enable Codex agents and sandbox tools; direct API chat does not require it.",
          required=False)
    image_ready = bool(daemon_ready and probe([docker, "image", "inspect", IMAGE, "--format", "{{.Id}}"]))
    check(f"Agent image {IMAGE}", image_ready,
          f"With Docker running: docker build -t {IMAGE} docker/codex-agent", required=False)
    from vwb.code.runtime import IMAGE as CODE_IMAGE
    code_ready = bool(daemon_ready and probe([docker, "image", "inspect", CODE_IMAGE, "--format", "{{.Id}}"]))
    check(f"Repository tools {CODE_IMAGE}", code_ready,
          "Run uv run python -m vwb.code.runtime install for read-only search, Git and Python/TypeScript navigation.",
          required=False)
    browser = browser_status()
    check("Pinned shared-browser runtime", browser["installed"],
          "Run uv run python -m vwb.browser.runtime install to enable shared browsing.", required=False)
    remote_browser = bool(os.environ.get("VWB_BROWSER_CDP_URL"))
    chrome = any(shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))
    if platform.system() == "Darwin":
        chrome = chrome or any((base / "Google Chrome.app/Contents/MacOS/Google Chrome").is_file()
                               for base in (Path("/Applications"), Path.home() / "Applications"))
    check("Chrome or remote CDP configured", bool(chrome or remote_browser),
          "Install Chrome on the control-plane host or set VWB_BROWSER_CDP_URL for a remote browser.", required=False)
    if platform.system() == "Linux" and not remote_browser:
        check("Linux display for headed browsing", bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
              "Headed browsing needs a display server; headless browsing remains available.", required=False)
    silicon = platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}
    index_ready = silicon and all(importlib.util.find_spec(name) is not None for name in ("mlx_whisper", "Vision", "Quartz"))
    check("Full source-video indexing (Apple Silicon, MLX Whisper and Apple Vision)", index_ready,
          "Full source-video indexing currently requires an Apple Silicon Mac. Chat, workspace previews and direct APIs can run elsewhere.",
          required=False)
    main = slot("main")
    console.print(f"Main slot: {main['model']} via {main['host']}", markup=False, highlight=False)
    console.print("Checks are local and read-only; provider keys are not validated with paid model calls.",
                  markup=False, highlight=False)
    if failed:
        raise typer.Exit(code=1)


@mcp_app.command("serve")
def mcp_serve(request: str = typer.Option(...), role: str = typer.Option("critic"), agent: str = typer.Option("mcp-client")):
    """Serve the toolkit over stdio MCP for an external host, limited by role."""
    from vwb.mcp_server import main
    main(request, role, agent)


if __name__ == "__main__":
    app()
