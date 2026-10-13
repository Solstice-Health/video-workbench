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
         attach: list[Path] = typer.Option(None, "--attach", "-a", help="Source video(s)."),
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
def doctor():
    """Check the local environment: binaries, generator, keys, models."""
    import shutil
    from vwb.config import find_env_file, secrets, slot
    from vwb.render.plates import REMOTION_DIR
    checks = {
        "ffmpeg": bool(shutil.which("ffmpeg")), "node": bool(shutil.which("node")),
        "remotion generator installed": (REMOTION_DIR / "node_modules" / "remotion").exists(),
        "fonts pinned": (REMOTION_DIR / "public" / "fonts").exists(),
        f"env file ({find_env_file()})": find_env_file() is not None,
        "OPENAI_API_KEY": bool(secrets().openai), "GOOGLE_API_KEY": bool(secrets().google),
        "OPENROUTER_API_KEY (optional web tools)": bool(secrets().openrouter),
    }
    for k, ok in checks.items():
        console.print(f"{'[green]ok[/]' if ok else '[red]missing[/]'}  {k}")
    console.print(f"main slot: {slot('main')['model']} via {slot('main')['host']} (local Claude login)")


@mcp_app.command("serve")
def mcp_serve(request: str = typer.Option(...), role: str = typer.Option("critic"), agent: str = typer.Option("mcp-client")):
    """Serve the toolkit over stdio MCP for an external host, limited by role."""
    from vwb.mcp_server import main
    main(request, role, agent)


if __name__ == "__main__":
    app()
