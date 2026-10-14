"""Explicit, model-free acceptance against the installed native browser.

Run from the repository: .venv/bin/python scripts/browser_smoke.py --mode both
Starts only owned Chrome sessions and a loopback fixture server, then closes them
in finally blocks. Results and screenshots go to a unique ignored runs directory.
This is an integration exercise, not part of the default unit-test suite. Remote
CDP is deliberately excluded: local files and remote lifecycle need a provider's
own acceptance environment. No external sites, accounts or model calls are used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import re
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from vwb.browser.service import BrowserSession
from vwb.tools.base import Registry, ToolContext
from vwb.tools.browser_tools import TOOLS
from vwb.workspace import Request, Workspace
from vwb.workspace.request import SUBDIRS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "browser"


@contextmanager
def fixture_server():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES), **kwargs)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class Smoke:
    def __init__(self, directory: Path, mode: str, origin: str):
        self.directory, self.mode, self.origin = directory, mode, origin
        self.ws = Workspace(directory / f"r-browser-{mode}")
        for sub in SUBDIRS:
            (self.ws.root / sub).mkdir(parents=True, exist_ok=True)
        self.ws.save(Request(id=self.ws.root.name, message="Explicit native browser acceptance; no generation."))
        self.session = BrowserSession(self.ws)
        self.registry = Registry(TOOLS)
        self.ctx = ToolContext(self.ws, agent="job_lead", role="job_lead", gateway=None)
        self.observation: dict = {}
        self.results: list[dict] = []
        self.calls = 0

    def record(self, name: str, **evidence) -> None:
        self.results.append({"check": name, "status": "passed", "at": time.time(), **evidence})
        print(f"{self.mode}: {name}", flush=True)

    def tool(self, name: str, args: dict, *, expected_error: str | None = None):
        result = self.registry.call(self.ctx, name, args)
        self.calls += 1
        if expected_error:
            assert result.is_error and expected_error.lower() in result.text.lower(), result.text
            return result
        if result.is_error:
            (self.directory / f"{self.mode}-unexpected-tool-error.json").write_text(json.dumps(
                {"tool": name, "args": args, "text": result.text, "data": result.data}, indent=2) + "\n")
        assert not result.is_error, result.text
        assert isinstance(result.data, dict), result.text
        if "observation_id" in result.data:
            self.observation = result.data
        return result

    def act(self, action: str, **args):
        return self.tool("browser_act", {"observation_id": self.observation["observation_id"], "action": action, **args})

    def ref(self, label: str) -> str:
        lines = [line for line in self.observation.get("snapshot", "").splitlines() if f'"{label}"' in line]
        found = [match.group(1) for line in lines if (match := re.search(r"\bref=(e\d+)\b", line))]
        assert len(found) == 1, f"Expected one reference for {label!r}, found {lines!r}"
        return "@" + found[0]

    def contains(self, text: str) -> None:
        assert text in self.observation.get("snapshot", ""), f"Missing {text!r} in snapshot: {self.observation}"

    def screenshot(self, result, label: str) -> dict:
        assert result.images, "No screenshot returned through Registry"
        raw = result.images[0]
        image = Image.open(io.BytesIO(raw))
        assert image.width > 0 and image.height > 0
        path = self.directory / f"{self.mode}-{label}.png"
        # Registry may encode JPEG when fitting; preserve the correct extension.
        if image.format == "JPEG":
            path = path.with_suffix(".jpg")
        path.write_bytes(raw)
        return {"path": str(path.relative_to(self.directory)), "sha256": hashlib.sha256(raw).hexdigest(),
                "width": image.width, "height": image.height, "format": image.format}

    def stream(self) -> None:
        outcome: dict = {"frames": []}

        def receive():
            iterator = self.session.stream()
            try:
                for kind, event in iterator:
                    if kind != "frame":
                        continue
                    raw = base64.b64decode(event["data"], validate=True)
                    with Image.open(io.BytesIO(raw)) as image:
                        assert image.format == "JPEG" and image.size == (1280, 800)
                    outcome["frames"].append({"seq": event["seq"], "sha256": hashlib.sha256(raw).hexdigest(),
                                               "bytes": len(raw), "metadata": event["metadata"]})
                    self.session.ui_action({"action": "frame_ack", "viewer_id": event["viewer_id"], "seq": event["seq"]})
                    if len(outcome["frames"]) == 2:
                        return
            except Exception as exc:
                outcome["error"] = str(exc)
            finally:
                iterator.close()

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        thread.join(timeout=15)
        assert not thread.is_alive(), "Live stream did not deliver two acknowledged frames within 15s"
        assert len(outcome["frames"]) == 2 and "error" not in outcome, outcome
        assert outcome["frames"][1]["seq"] > outcome["frames"][0]["seq"]
        self.record("live broker JPEG frames and acknowledgements", transport="service iterator; HTTP routing tested separately", **outcome)

    def stream_tabs(self) -> None:
        """Keep one broker subscription alive across static/blank tab switches."""
        condition, stop = threading.Condition(), threading.Event()
        received: list[dict] = []
        failure: list[str] = []

        def receive():
            iterator = self.session.stream()
            try:
                for kind, event in iterator:
                    if stop.is_set():
                        return
                    if kind == "frame":
                        raw = base64.b64decode(event["data"], validate=True)
                        with Image.open(io.BytesIO(raw)) as picture:
                            darkest = min(lo for lo, _hi in picture.convert("RGB").getextrema())
                        with condition:
                            received.append({"at": time.time(), "seq": event["seq"], "tab_id": event.get("tab_id"),
                                             "sha256": hashlib.sha256(raw).hexdigest(), "darkest_channel": darkest})
                            condition.notify_all()
                        self.session.ui_action({"action": "frame_ack", "viewer_id": event["viewer_id"], "seq": event["seq"]})
            except Exception as exc:
                failure.append(str(exc))
            finally:
                iterator.close()

        def wait_frame(tab_id: str, after: float, *, blank: bool = False) -> dict:
            deadline = time.monotonic() + 5
            with condition:
                while time.monotonic() < deadline:
                    choices = [r for r in received if r["tab_id"] == tab_id and r["at"] >= after]
                    if choices:
                        row = choices[-1]
                        assert (row["darkest_channel"] >= 250) if blank else (row["darkest_channel"] < 200), row
                        return row
                    condition.wait(timeout=max(0, deadline - time.monotonic()))
            raise AssertionError(f"No frame for tab {tab_id} within 5s after switch; received={received[-10:]}; errors={failure}")

        thread = threading.Thread(target=receive, daemon=True)
        thread.start()
        transitions = []
        try:
            original = self.observation["tab_id"]
            wait_frame(original, 0)
            self.session.ui_action({"action": "control", "owner": "human"})
            began = time.time()
            tabs = self.session.ui_action({"action": "tab", "operation": "new", "url": "about:blank"})["tabs"]
            blank = next(t["id"] for t in tabs if t["active"])
            transitions.append({"operation": "new blank", "frame": wait_frame(blank, began, blank=True)})
            for iteration in range(3):
                for target, is_blank in ((original, False), (blank, True)):
                    began = time.time()
                    self.session.ui_action({"action": "tab", "operation": "select", "tab_id": target})
                    transitions.append({"operation": "select", "iteration": iteration, "frame": wait_frame(target, began, blank=is_blank)})
            self.session.ui_action({"action": "tab", "operation": "select", "tab_id": original})
            self.session.ui_action({"action": "tab", "operation": "close", "tab_id": blank})
            self.record("continuous stream survives blank/static tab switches", transitions=transitions, frames=len(received))
        finally:
            stop.set()
            self.session.ui_action({"action": "control", "owner": "agent"})
            thread.join(timeout=6)
            assert not thread.is_alive(), "Continuous stream receiver did not release its connection"
            (self.directory / f"{self.mode}-stream-tabs.json").write_text(json.dumps(
                {"frames": received, "transitions": transitions, "errors": failure}, indent=2) + "\n")
        self.tool("browser_observe", {})

    def run_stream_tabs(self) -> dict:
        began, failed = time.time(), None
        try:
            self.tool("browser_open", {"url": self.origin + "/child.html", "mode": self.mode})
            self.stream_tabs()
        except Exception as exc:
            failed = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            print(f"{self.mode}: FAILED: {exc}", flush=True)
        finally:
            self.session.ui_action({"action": "close"})
            assert not self.session.driver.alive()
            self.record("owned browser and daemon closed")
        return {"mode": self.mode, "status": "failed" if failed else "passed", "started": began, "ended": time.time(),
                "request": str(self.ws.root), "registry_calls": self.calls, "checks": self.results, "failure": failed}

    def run(self) -> dict:
        began = time.time()
        failed = None
        try:
            opened = self.tool("browser_open", {"url": self.origin + "/index.html", "mode": self.mode})
            self.contains("Ordinary paragraph evidence must survive")
            assert self.observation["observation_id"] and self.observation["viewport"] == {"width": 1280, "height": 800}
            original_tab = self.observation["tab_id"]
            assert re.fullmatch(r"t\d+", original_tab)
            original_session = self.observation["session_id"]
            self.record("Registry open, full snapshot, screenshot and stable tab ID", image=self.screenshot(opened, "opened"))
            self.stream()

            stale = self.observation["observation_id"]
            self.act("fill", ref=self.ref("Your name"), text="Workbench visitor")
            self.act("press", key="Control+a")
            self.act("press", key="Backspace")
            assert "Workbench visitor" not in self.observation["snapshot"]
            self.record("agent press Select all deletes the full input value")
            self.act("fill", ref=self.ref("Your name"), text="Workbench visitor")
            result = self.act("click", ref=self.ref("Save name"))
            self.contains("Saved: Workbench visitor")
            cursor = self.session.status()["cursor"]
            assert cursor["visible"] and cursor["action"] == "completed"
            self.record("fill and click with main-frame cursor", cursor=cursor, image=self.screenshot(result, "filled"))
            self.tool("browser_act", {"observation_id": stale, "action": "click", "ref": self.ref("Save name")}, expected_error="stale")
            self.record("consumed observation cannot replay")

            self.act("select", ref=self.ref("Colour"), text="blue")
            self.act("check", ref=self.ref("Enable option"))
            self.contains('[checked=true,')
            self.record("native select and checkbox")

            self.act("click_at", x=920, y=160)
            self.contains("Canvas clicks: 1")
            cursor = self.session.status()["cursor"]
            assert cursor["visible"] and cursor["x"] == 920 and cursor["y"] == 160
            self.record("visual canvas coordinate click", cursor=cursor)

            self.act("tab", operation="new", url=self.origin + "/child.html")
            sibling = self.observation["tab_id"]
            assert sibling != original_tab and len(self.observation["tabs"]) == 2
            self.act("tab", operation="select", tab_id=original_tab)
            self.contains("Saved: Workbench visitor")
            self.act("tab", operation="close", tab_id=sibling)
            assert self.observation["tab_id"] == original_tab and len(self.observation["tabs"]) == 1
            self.record("stable tab switching and non-active sibling close", original_tab=original_tab, closed_tab=sibling)

            upload = self.ws.work / "upload-fixture.txt"
            upload.write_text("Deterministic local upload fixture.\n", encoding="utf-8")
            self.tool("browser_files", {"observation_id": self.observation["observation_id"], "action": "upload",
                                         "ref": self.ref("Upload fixture"), "path": "work/upload-fixture.txt"})
            self.contains("Selected file: upload-fixture.txt")
            self.record("native file input upload", bytes=upload.stat().st_size)
            downloaded = self.tool("browser_files", {"observation_id": self.observation["observation_id"], "action": "download",
                                                      "ref": self.ref("Download fixture"), "filename": "fixture.txt"})
            evidence = downloaded.data["download"]
            payload = (self.ws.root / evidence["path"]).read_bytes()
            assert payload == (FIXTURES / "download.txt").read_bytes()
            assert evidence["sha256"] == hashlib.sha256(payload).hexdigest()
            self.record("native download imported with exact bytes and provenance", download=evidence)

            self.act("click", ref=self.ref("Open prompt"))
            assert self.observation.get("dialog", {}).get("type") == "prompt", self.observation
            assert not self.observation["observation_id"], "A blocked dialog cannot return an actionable page observation"
            self.tool("browser_dialog", {"response": "accept", "text": "Smoke answer"})
            self.contains("Prompt answer: Smoke answer")
            self.record("native prompt observed and answered")

            before_handoff = self.observation["observation_id"]
            self.session.ui_action({"action": "control", "owner": "human"})
            self.tool("browser_observe", {}, expected_error="control")
            self.tool("browser_act", {"observation_id": before_handoff, "action": "click_at", "x": 920, "y": 160}, expected_error="control")
            self.tool("browser_close", {}, expected_error="control")
            self.session.ui_action({"action": "input", "events": [
                {"type": "input_mouse", "eventType": "mouseMoved", "x": 920, "y": 160},
                {"type": "input_mouse", "eventType": "mousePressed", "x": 920, "y": 160, "button": "left"},
                {"type": "input_mouse", "eventType": "mouseReleased", "x": 920, "y": 160, "button": "left"},
            ]})
            self.session.ui_action({"action": "control", "owner": "agent"})
            self.tool("browser_act", {"observation_id": before_handoff, "action": "click_at", "x": 920, "y": 160}, expected_error="stale")
            self.tool("browser_observe", {})
            self.contains("Canvas clicks: 2")
            self.record("human takeover blocks agent, native input delivered, return requires fresh observation")

            self.act("click", ref=self.ref("Go to bottom"))
            self.act("click", ref=self.ref("Bottom button"))
            self.contains("Bottom clicked")
            self.record("scrolling to and clicking below-fold control")

            self.act("navigate", url=self.origin + "/frames.html")
            self.act("click", ref=self.ref("Frame button"))
            self.contains("Frame clicked")
            assert not self.session.status()["cursor"]["visible"], "Frame-local boxes must not paint wrong viewport cursors"
            self.record("iframe reference clicks correct child and suppresses unverified cursor")
            self.act("frame", selector="#fixture-frame")
            self.act("fill", ref=self.ref("Name inside frame"), text="Inside child")
            self.contains("Inside child")
            self.session.ui_action({"action": "control", "owner": "human"})
            self.session.ui_action({"action": "input", "events": [
                {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 4},
                {"type": "input_keyboard", "eventType": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 4},
                {"type": "input_keyboard", "eventType": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
                {"type": "input_keyboard", "eventType": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
            ]})
            self.session.ui_action({"action": "control", "owner": "agent"})
            self.tool("browser_observe", {})
            assert "Inside child" not in self.observation["snapshot"]
            self.record("human keyboard Select all deletes full focused iframe input")
            self.act("frame", selector="main")
            self.contains("Frame acceptance")
            self.record("explicit frame selection, fill, and return to main")

            self.act("navigate", url=self.origin + "/index.html")
            self.act("click_at", x=920, y=160)
            self.contains("Canvas clicks: 1")
            disconnected_observation = self.observation["observation_id"]
            asyncio.run(self.session.driver.stop())
            assert not self.session.driver.alive()
            self.tool("browser_act", {"observation_id": disconnected_observation, "action": "click_at", "x": 920, "y": 160}, expected_error="disconnected")
            assert not self.session.driver.alive(), "A rejected action must not automatically restart the browser"
            self.tool("browser_open", {"url": self.origin + "/index.html", "mode": self.mode})
            assert self.observation["session_id"] != original_session
            self.contains("Canvas clicks: 0")
            self.tool("browser_act", {"observation_id": disconnected_observation, "action": "click_at", "x": 920, "y": 160}, expected_error="stale")
            self.record("native disconnect rejects without restart/replay; explicit reopen starts a fresh observation")

            rows = [json.loads(line) for line in (self.ws.ledger / "tools.jsonl").read_text().splitlines()]
            assert len(rows) == self.calls
            assert any(not row["ok"] for row in rows) and any(row["ok"] for row in rows)
            self.record("all Registry calls recorded, including refused stale/ownership actions", calls=len(rows))
        except Exception as exc:
            failed = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            print(f"{self.mode}: FAILED: {exc}", flush=True)
        finally:
            try:
                self.session.ui_action({"action": "close"})
                assert not self.session.driver.alive()
                self.record("owned browser and daemon closed")
            except Exception as exc:
                cleanup = {"type": type(exc).__name__, "message": str(exc)}
                failed = {**(failed or {}), "cleanup_error": cleanup}
        return {"mode": self.mode, "status": "failed" if failed else "passed", "started": began, "ended": time.time(),
                "request": str(self.ws.root), "registry_calls": self.calls, "checks": self.results, "failure": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("headed", "headless", "both"), default="both")
    parser.add_argument("--stream-tabs-only", action="store_true", help="Keep a continuous stream across repeated blank/static tab switches.")
    parser.add_argument("--output", type=Path, help="New output directory, normally under ignored runs/.")
    args = parser.parse_args()
    directory = args.output or ROOT / "runs" / "browser-research" / f"service-smoke-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    modes = ("headed", "headless") if args.mode == "both" else (args.mode,)
    results = {"started": time.time(), "kind": "explicit native browser acceptance", "model_calls": 0, "results": []}
    with fixture_server() as origin:
        for mode in modes:
            smoke = Smoke(directory, mode, origin)
            results["results"].append(smoke.run_stream_tabs() if args.stream_tabs_only else smoke.run())
            (directory / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    results["ended"] = time.time()
    results["fixture_server_closed"] = True
    results["status"] = "passed" if all(row["status"] == "passed" for row in results["results"]) else "failed"
    (directory / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"Report: {directory / 'result.json'}", flush=True)
    return int(results["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
