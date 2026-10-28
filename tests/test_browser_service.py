"""Independent session/ownership contract checks with an in-memory browser driver."""

from __future__ import annotations

import io
import json
import threading
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from vwb.browser.service import OBSERVATION_TTL, BrowserConflict, BrowserSession
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.is_alive = False
        self.doc = {"document": "doc-one", "url": "https://example.test/", "width": 1280,
                    "height": 800, "scrollX": 0, "scrollY": 0, "layout": 0}
        self.pages = [{"id": "t1", "url": self.doc["url"], "title": "Test page", "active": True}]
        self.after_snapshot = None
        self.action_error = None
        self.action_entered = None
        self.action_release = None
        self.download_bytes = b"download fixture"
        self.dialog_status = {"hasDialog": False}
        self.snapshot_text = '- button "Continue" [ref=e1]\n- textbox "Name" [ref=e2]'
        output = io.BytesIO()
        Image.new("RGB", (1280, 800), "white").save(output, format="PNG")
        self.png = output.getvalue()

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        self.is_alive = True
        return {}

    async def stop(self):
        if not self.is_alive:
            self.calls.append(("stop_absent",))
            return {"closed": True, "was_running": False}
        self.calls.append(("stop",))
        self.is_alive = False
        return {}

    def alive(self):
        return self.is_alive

    async def stream_port(self):
        self.calls.append(("stream_port",))
        return 54321

    async def tabs(self):
        self.calls.append(("tabs",))
        return deepcopy(self.pages)

    async def snapshot(self, **kwargs):
        self.calls.append(("snapshot", kwargs))
        if self.after_snapshot:
            self.after_snapshot()
        return {"snapshot": self.snapshot_text, "refs": {"e1": {}, "e2": {}}}

    async def screenshot(self, **kwargs):
        self.calls.append(("screenshot", kwargs))
        return SimpleNamespace(data=self.png, annotations=[])

    async def run(self, args, **kwargs):
        self.calls.append(("run", list(args)))
        if args[0] == "eval":
            return {"result": deepcopy(self.doc)}
        if args == ["dialog", "status"]:
            return deepcopy(self.dialog_status)
        if args == ["get", "url"]:
            return {"url": self.doc["url"]}
        if self.action_entered:
            self.action_entered.set()
            assert self.action_release.wait(5), "test did not release the fake browser action"
        if self.action_error:
            raise self.action_error
        if args[0] == "open":
            self.doc.update(document="doc-navigated", url=args[1])
            self.pages[0]["url"] = args[1]
        if args[:2] == ["tab", "close"]:
            self.pages = [page for page in self.pages if page["id"] != args[2]]
        if args[0] == "download":
            Path(args[2]).write_bytes(self.download_bytes)
        if args[0] == "dialog" and args[1] in {"accept", "dismiss"}:
            self.dialog_status = {"hasDialog": False}
        return {"ok": True}


@pytest.fixture
def browser(tmp_path):
    ws = Workspace(tmp_path / "r-browser-service")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.root.name, message="Inspect a page"))
    driver = FakeDriver()
    session = BrowserSession(ws, driver=driver)
    return session, driver


def started(browser):
    session, driver = browser
    session.ui_action({"action": "start", "mode": "headed"})
    return session, driver


def observation(browser, *, actor="job_lead"):
    session, driver = started(browser)
    state, images = session.observe(actor=actor, screenshot=False)
    assert images == []
    return session, driver, state


def commands(driver):
    return [args for kind, *rest in driver.calls if kind == "run" and (args := rest[0])[0] != "eval"
            and args not in (["dialog", "status"], ["get", "url"])]


def test_status_and_stopped_stream_do_not_launch_or_contact_browser(browser):
    session, driver = browser
    assert session.status()["state"] == "stopped"
    assert list(session.stream())[0][0] == "state"
    assert driver.calls == []


def test_start_modes_need_explicit_close_before_switching(browser):
    session, driver = started(browser)
    original = session.status()["session_id"]
    session.ui_action({"action": "start", "mode": "headed"})
    assert len([call for call in driver.calls if call[0] == "start"]) == 1
    with pytest.raises(BrowserConflict):
        session.ui_action({"action": "start", "mode": "headless"})
    assert session.status()["session_id"] == original
    session.ui_action({"action": "close"})
    session.ui_action({"action": "start", "mode": "headless"})
    assert session.status()["session_id"] != original
    assert session.status()["mode"] == "headless"


@pytest.mark.parametrize("body", [
    {"action": "navigate", "url": "https://example.test/new"},
    {"action": "tab", "operation": "new"},
    {"action": "input", "events": []},
    {"action": "reload"},
])
def test_human_mutation_is_rejected_without_human_lease(browser, body):
    session, driver = started(browser)
    driver.calls.clear()
    with pytest.raises(BrowserConflict, match="Take control"):
        session.ui_action(body)
    assert driver.calls == []


def test_handoff_rejects_agent_work_and_return_requires_fresh_observation(browser):
    session, driver, old = observation(browser)
    session.ui_action({"action": "control", "owner": "human"})
    driver.calls.clear()
    with pytest.raises(BrowserConflict):
        session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"]}, actor="job_lead")
    with pytest.raises(BrowserConflict):
        session.observe(actor="job_lead")
    assert driver.calls == []
    session.ui_action({"action": "control", "owner": "agent"})
    with pytest.raises(BrowserConflict, match="stale"):
        session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"]}, actor="job_lead")
    fresh, _ = session.observe(actor="job_lead", screenshot=False)
    result, _ = session.act({"action": "click", "ref": "e1", "observation_id": fresh["observation_id"],
                             "screenshot": False}, actor="job_lead")
    assert result["action_completed"]
    assert commands(driver) == [["click", "@e1"]]


def test_takeover_acknowledges_only_after_inflight_action_finishes(browser):
    session, driver, observed = observation(browser)
    other = BrowserSession(session.ws, driver=driver)
    driver.action_entered, driver.action_release = threading.Event(), threading.Event()
    finished, errors = threading.Event(), []

    def act():
        try:
            session.act({"action": "click", "ref": "e1", "observation_id": observed["observation_id"],
                         "screenshot": False}, actor="job_lead")
        except Exception as exc:
            errors.append(exc)

    def takeover():
        try:
            other.ui_action({"action": "control", "owner": "human"})
            finished.set()
        except Exception as exc:
            errors.append(exc)

    acting = threading.Thread(target=act)
    handing_off = threading.Thread(target=takeover)
    acting.start()
    try:
        assert driver.action_entered.wait(3)
        handing_off.start()
        assert not finished.wait(.1)
        assert other.status()["owner"] == "agent"
    finally:
        driver.action_release.set()
        acting.join(timeout=5)
        if handing_off.ident is not None:
            handing_off.join(timeout=5)
    assert not acting.is_alive() and not handing_off.is_alive()
    assert errors == [] and finished.is_set()
    assert session.status()["owner"] == "human"
    assert "observation" not in session._read()


@pytest.mark.parametrize("change", ["document", "scrollY", "width", "tab"])
def test_action_rejects_page_or_tab_changes_since_observation(browser, change):
    session, driver, old = observation(browser)
    if change == "tab":
        driver.pages = [{"id": "t2", "active": True, "title": "Same URL, other tab", "url": driver.doc["url"]}]
    else:
        driver.doc[change] = "doc-two" if change == "document" else driver.doc[change] + 100
    with pytest.raises(BrowserConflict, match="changed"):
        session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"]}, actor="job_lead")
    assert commands(driver) == []
    assert "observation" not in session._read()


def test_observation_changed_during_capture_is_not_published(browser):
    session, driver = started(browser)
    driver.after_snapshot = lambda: driver.doc.update(document="doc-two")
    with pytest.raises(BrowserConflict, match="moved"):
        session.observe(actor="job_lead", screenshot=False)
    assert "observation" not in session._read()


@pytest.mark.parametrize("kind", ["wrong_id", "wrong_actor", "unknown_ref", "expired"])
def test_actions_require_owned_current_unexpired_references(browser, monkeypatch, kind):
    session, driver, old = observation(browser)
    body = {"action": "click", "ref": "e1", "observation_id": old["observation_id"]}
    actor = "job_lead"
    if kind == "wrong_id":
        body["observation_id"] = "invented"
    elif kind == "wrong_actor":
        actor = "worker"
    elif kind == "unknown_ref":
        body["ref"] = "e99"
    else:
        monkeypatch.setattr("vwb.browser.service.time.time", lambda: old["observed_at"] + OBSERVATION_TTL + 1)
    with pytest.raises(BrowserConflict):
        session.act(body, actor=actor)
    assert commands(driver) == []


def test_ambiguous_failed_action_consumes_observation_and_is_not_retried(browser):
    session, driver, old = observation(browser)
    body = {"action": "click", "ref": "e1", "observation_id": old["observation_id"]}
    driver.action_error = RuntimeError("Command timed out after dispatch")
    with pytest.raises(RuntimeError, match="timed out"):
        session.act(body, actor="job_lead")
    with pytest.raises(BrowserConflict, match="stale"):
        session.act(body, actor="job_lead")
    assert commands(driver) == [["click", "@e1"]]


@pytest.mark.parametrize("body", [
    {"action": "eval", "javascript": "fetch('https://unrelated.example')"},
    {"action": "run", "command": "auth export"},
    {"action": "navigate", "url": "file:///etc/passwd"},
    {"action": "navigate", "url": "javascript:alert(1)"},
    {"action": "navigate", "url": "https://user:secret@example.test/"},
    {"action": "tab", "operation": "select", "tab_id": 2},
    {"action": "click", "ref": "button[name=Submit]"},
])
def test_agent_action_allowlist_rejects_code_privileged_urls_and_guessed_targets(browser, body):
    session, driver, old = observation(browser)
    with pytest.raises(ValueError):
        session.act(dict(body, observation_id=old["observation_id"]), actor="job_lead")
    assert commands(driver) == []


def test_public_status_hides_runtime_endpoints_and_private_observation(browser):
    session, _ = started(browser)
    state = session._read()
    state.update(stream_port=54321, cdp_url="wss://private.invalid/?token=hidden",
                 private_path="/private/secret/profile", observation={"refs": ["secret-ref"], "id": "private-observation"})
    session._save(state)
    public = json.dumps(session.status())
    for secret in ("54321", "private.invalid", "hidden", "/private/secret", "secret-ref", "private-observation"):
        assert secret not in public


def test_closing_sibling_does_not_change_active_stable_tab_identity(browser):
    session, driver = started(browser)
    driver.pages = [{"id": "t1", "url": "https://example.test/one", "active": False},
                    {"id": "t3", "url": driver.doc["url"], "title": "Third", "active": True}]
    session.ui_action({"action": "control", "owner": "human"})
    session.ui_action({"action": "tab", "operation": "close", "tab_id": "t1"})
    assert session.status()["tabs"] == [{"id": "t3", "url": driver.doc["url"], "title": "Third", "active": True}]
    session.ui_action({"action": "control", "owner": "agent"})
    state, _ = session.observe(actor="job_lead", screenshot=False)
    assert state["tab_id"] == "t3"


def test_snapshot_budget_discloses_truncation_and_untrusted_content(browser):
    session, driver = started(browser)
    driver.snapshot_text = "a" * 25000
    state, _ = session.observe(actor="job_lead", screenshot=False)
    assert len(state["snapshot"]) == 24000 and state["truncated"]
    assert "untrusted" in state["notice"] and "frame" in state["notice"]


def test_close_invalidates_observation_and_stops_once(browser):
    session, driver, old = observation(browser)
    session.ui_action({"action": "close"})
    session.ui_action({"action": "close"})
    assert len([call for call in driver.calls if call[0] == "stop"]) == 1
    assert session.status()["state"] == "stopped"
    assert "stream_port" not in session._read() and "observation" not in session._read()
    with pytest.raises(BrowserConflict):
        session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"]}, actor="job_lead")


def test_dead_daemon_does_not_restart_during_status_or_action(browser):
    session, driver, old = observation(browser)
    driver.is_alive = False
    driver.calls.clear()
    assert session.status()["state"] == "error"
    with pytest.raises(BrowserConflict, match="disconnected"):
        session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"]}, actor="job_lead")
    assert driver.calls == []
    session.ui_action({"action": "start", "mode": "headed"})
    assert session.status()["session_id"] != old["session_id"]
    assert "observation" not in session._read()


@pytest.mark.parametrize("invalid", [
    {"type": "input_mouse", "eventType": "mouseMoved", "x": float("nan"), "y": 10},
    {"type": "input_mouse", "eventType": "mousePressed", "x": 1300, "y": 10},
    {"type": "input_mouse", "eventType": "mousePressed", "x": 10, "y": 10, "button": "back"},
    {"type": "input_keyboard", "eventType": "eval", "text": "alert(1)"},
    {"type": "input_keyboard", "eventType": "char", "text": "x" * 10001},
    {"type": "input_keyboard", "eventType": "char", "text": "x", "modifiers": 16},
])
def test_invalid_input_batch_sends_no_partial_events(browser, monkeypatch, invalid):
    session, _ = started(browser)
    session.ui_action({"action": "control", "owner": "human"})
    connections = []
    monkeypatch.setattr("vwb.browser.service.connect", lambda *args, **kwargs: connections.append(args))
    valid = {"type": "input_mouse", "eventType": "mouseMoved", "x": 10, "y": 10}
    with pytest.raises(ValueError):
        session.ui_action({"action": "input", "events": [valid, invalid]})
    assert connections == []


def test_human_input_forwards_only_typed_fields_and_fences_delivery(browser, monkeypatch):
    session, _ = started(browser)
    session.ui_action({"action": "control", "owner": "human"})
    sent, pinged = [], []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def send(self, value):
            sent.append(json.loads(value))

        def ping(self):
            pinged.append(True)
            return SimpleNamespace(wait=lambda timeout: True)

        def recv(self, **kwargs):
            raise ValueError("No incoming messages in this fixture")

        def close(self):
            return None

    monkeypatch.setattr("vwb.browser.service.connect", lambda *args, **kwargs: Socket())
    session.ui_action({"action": "input", "events": [{"type": "input_keyboard", "eventType": "char",
                       "text": "private typed value", "modifiers": 0, "untrusted_extra": "not forwarded"}]})
    assert sent == [{"type": "input_keyboard", "eventType": "char", "text": "private typed value", "modifiers": 0}]
    assert pinged == [True]
    assert "private typed value" not in (session.private / "audit.jsonl").read_text()


def test_frame_ack_cannot_acknowledge_another_request_viewer(browser, monkeypatch):
    from vwb.browser import service

    session, _ = browser
    foreign = service._Viewer("another-request")
    foreign.sent = 5
    own = service._Viewer(session.key)
    own.sent = 5
    monkeypatch.setattr(service, "_viewers", {"foreign": foreign, "own": own})
    session.ui_action({"action": "frame_ack", "viewer_id": "foreign", "seq": 5})
    session.ui_action({"action": "frame_ack", "viewer_id": "own", "seq": 6})
    assert foreign.ack == own.ack == -1
    session.ui_action({"action": "frame_ack", "viewer_id": "own", "seq": 5})
    assert own.ack == 5


def test_stream_never_forwards_native_command_payloads_and_cleans_viewer(browser, monkeypatch):
    from vwb.browser import service

    session, _ = started(browser)
    closed, sent = [], []
    native = iter([
        {"type": "command", "text": "private typed value", "cdp_url": "wss://private.invalid"},
        {"type": "frame", "seq": 7, "data": "encoded-frame", "metadata": {"deviceWidth": 1280}},
    ])

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)
            return False

        def recv(self, **kwargs):
            return json.dumps(next(native))

        def send(self, value):
            sent.append(json.loads(value))

    monkeypatch.setattr(service, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(service, "_viewers", {})
    stream = session.stream()
    assert next(stream)[0] == "state"
    kind, data = next(stream)
    assert kind == "frame" and data["data"] == "encoded-frame"
    assert "private" not in json.dumps(data)
    assert data["viewer_id"] in service._viewers
    stream.close()
    assert closed == [True] and service._viewers == {}
    assert sent == []  # Native frame is not acknowledged before the renderer.


@pytest.mark.parametrize("kind", ["file", "directory", "outside"])
def test_upload_rejects_symlink_components_before_driver_dispatch(browser, tmp_path, kind):
    session, driver, old = observation(browser)
    secret = session.private / "private.txt"
    secret.write_text("private session material")
    if kind == "directory":
        (session.ws.work / "linked-directory").symlink_to(session.private, target_is_directory=True)
        path = "work/linked-directory/private.txt"
    else:
        if kind == "outside":
            secret = tmp_path / "outside.txt"
            secret.write_text("outside the request")
        (session.ws.work / "linked-file.txt").symlink_to(secret)
        path = "work/linked-file.txt"
    with pytest.raises(ValueError, match="symlink"):
        session.act({"action": "upload", "ref": "e1", "path": path,
                     "observation_id": old["observation_id"]}, actor="job_lead")
    assert commands(driver) == []
    assert secret.exists()


def test_upload_regular_named_workspace_file_is_permitted(browser):
    session, driver, old = observation(browser)
    path = session.ws.work / "selected.txt"
    path.write_text("user-selected content")
    result, _ = session.act({"action": "upload", "ref": "e1", "path": "work/selected.txt",
                             "observation_id": old["observation_id"], "screenshot": False}, actor="job_lead")
    assert result["action_completed"]
    sent = commands(driver)
    assert len(sent) == 1 and sent[0][:2] == ["upload", "@e1"]
    uploaded = Path(sent[0][2])
    assert uploaded.name == path.name and uploaded.is_relative_to(session.private)
    assert uploaded.read_text() == "user-selected content"
    path.write_text("later change")
    assert uploaded.read_text() == "user-selected content"  # browser submission stays pinned


@pytest.mark.parametrize("action", ["click_at", "move_at"])
def test_coordinate_action_rejects_layout_mutation_after_observation(browser, action):
    session, driver, old = observation(browser)
    driver.doc["layout"] += 1
    with pytest.raises(BrowserConflict, match="changed"):
        session.act({"action": action, "x": 20, "y": 30,
                     "observation_id": old["observation_id"]}, actor="job_lead")
    assert commands(driver) == []


def test_coordinate_click_dispatches_native_mouse_sequence(browser):
    session, driver, old = observation(browser)
    result, _ = session.act({"action": "click_at", "x": 20.2, "y": 30.8,
                             "observation_id": old["observation_id"], "screenshot": False}, actor="job_lead")
    assert result["action_completed"]
    assert commands(driver) == [["mouse", "move", "20", "31"], ["mouse", "down", "left"], ["mouse", "up", "left"]]


def test_reference_action_is_not_starved_by_unrelated_layout_mutations(browser):
    session, driver, old = observation(browser)
    driver.doc["layout"] += 1
    result, _ = session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"],
                             "screenshot": False}, actor="job_lead")
    assert result["action_completed"] and commands(driver) == [["click", "@e1"]]


@pytest.mark.parametrize("failure_stage", ["launch", "stream", "tabs"])
def test_partial_start_failure_closes_owned_daemon_and_publishes_recoverable_state(browser, monkeypatch, failure_stage):
    session, driver = browser

    async def fail(*args, **kwargs):
        driver.is_alive = True  # Failure can occur after the native process started.
        raise RuntimeError("fixture startup failed")

    method = {"launch": "start", "stream": "stream_port", "tabs": "tabs"}[failure_stage]
    monkeypatch.setattr(driver, method, fail)
    with pytest.raises(RuntimeError, match="startup failed"):
        session.ui_action({"action": "start", "mode": "headed"})
    assert driver.is_alive is False
    assert session.status()["state"] == "error"
    assert "observation" not in session._read()


def test_download_import_has_hash_size_and_untrusted_notice(browser):
    session, driver, old = observation(browser)
    result, _ = session.act({"action": "download", "ref": "e1", "filename": "result.txt",
                             "observation_id": old["observation_id"], "screenshot": False}, actor="job_lead")
    artifact = result["download"]
    assert artifact["path"].startswith("work/browser-downloads/")
    assert artifact["bytes"] == len(driver.download_bytes)
    assert artifact["sha256"] == sha256(driver.download_bytes).hexdigest()
    assert "Untrusted" in artifact["notice"]
    assert (session.ws.root / artifact["path"]).read_bytes() == driver.download_bytes
    assert list((session.private / "downloads").iterdir()) == []


@pytest.mark.parametrize("component", ["work", "browser-downloads"])
def test_download_cannot_import_through_symlinked_destination(browser, tmp_path, component):
    session, driver, old = observation(browser)
    outside = tmp_path / "outside-download-target"
    outside.mkdir()
    if component == "work":
        session.ws.work.rmdir()
        session.ws.work.symlink_to(outside, target_is_directory=True)
    else:
        (session.ws.work / "browser-downloads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        session.act({"action": "download", "ref": "e1", "filename": "result.txt",
                     "observation_id": old["observation_id"], "screenshot": False}, actor="job_lead")
    assert list(outside.iterdir()) == []


def test_native_dialog_observation_does_not_evaluate_blocked_page(browser):
    session, driver = started(browser)
    driver.dialog_status = {"hasDialog": True, "type": "prompt", "message": "Page prompt", "defaultPrompt": "Name"}
    driver.calls.clear()
    result, images = session.observe(actor="job_lead", screenshot=False)
    assert result["observation_id"] is None and images == []
    assert result["dialog"] == {"type": "prompt", "message": "Page prompt", "defaultPrompt": "Name"}
    assert "untrusted" in result["notice"]
    assert not any(call[0] == "snapshot" for call in driver.calls)
    assert all(call[0] != "run" or call[1][0] != "eval" for call in driver.calls)
    result, _ = session.dialog({"response": "dismiss"}, actor="job_lead")
    assert result["observation_id"] and "dialog" not in result
    assert ["dialog", "dismiss"] in commands(driver)


def test_stream_tab_events_do_not_overwrite_newer_tab_state_or_forward_stale_frames(browser, monkeypatch):
    from vwb.browser import service

    session, _ = started(browser)
    state = session._read()
    timestamp = state["tabs_updated_at"]
    second = {"id": "t2", "url": "https://example.test/two", "title": "Second", "active": True}
    native = iter([
        {"type": "tabs", "timestamp": (timestamp - 1) * 1000,
         "tabs": [{"tabId": "t1", "active": True, "title": "Stale", "url": "https://example.test/old"}]},
        {"type": "frame", "seq": 6, "data": "stale-frame"},
        {"type": "tabs", "timestamp": (timestamp + 1) * 1000,
         "tabs": [{"tabId": "t2", **second, "cdpUrl": "wss://private.invalid"}]},
        {"type": "frame", "seq": 7, "data": "current-frame"},
    ])
    sent, closed = [], []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)

        def recv(self, **kwargs):
            return json.dumps(next(native))

        def send(self, value):
            sent.append(json.loads(value))

    monkeypatch.setattr(service, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(service, "_viewers", {})
    stream = session.stream()
    assert next(stream)[0] == "state"
    state.update(tabs=[second], url=second["url"], title=second["title"])
    session._save(state)  # Represents a completed tab command newer than the queued native event.
    kind, current = next(stream)
    assert kind == "state" and current["tabs"] == [second]
    kind, frame = next(stream)
    assert kind == "frame" and frame["data"] == "current-frame" and frame["tab_id"] == "t2"
    assert frame["session_id"] == state["session_id"]
    assert sent == [{"type": "ack", "seq": 6}]
    assert "private.invalid" not in json.dumps(session.status())
    stream.close()
    assert closed == [True] and service._viewers == {}


def test_uncertain_prior_operation_is_fenced_before_human_takeover(browser, monkeypatch):
    session, driver, old = observation(browser)
    driver.action_error = RuntimeError("uncertain prior action")
    with pytest.raises(RuntimeError):
        session.act({"action": "click", "ref": "e1", "observation_id": old["observation_id"]}, actor="job_lead")
    assert session._read()["pending_operation"]["action"] == "click"
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = driver.run

    async def fenced(args, **kwargs):
        if args == ["dialog", "status"]:
            entered.set()
            assert release.wait(5), "test did not release the fake command queue fence"
        return await original(args, **kwargs)

    monkeypatch.setattr(driver, "run", fenced)

    def take_control():
        try:
            session.ui_action({"action": "control", "owner": "human"})
            finished.set()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=take_control)
    thread.start()
    try:
        assert entered.wait(3)
        assert not finished.is_set() and session.status()["owner"] == "agent"
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and not errors and finished.is_set()
    assert session.status()["owner"] == "human" and "pending_operation" not in session._read()
    assert commands(driver) == [["click", "@e1"]]


def test_pending_operation_recovery_works_while_native_prompt_blocks_javascript(browser, monkeypatch):
    session, driver, _ = observation(browser)
    driver.dialog_status = {"hasDialog": True, "type": "prompt", "message": "Pending prompt", "defaultPrompt": ""}
    state = session._read()
    state["pending_operation"] = {"action": "click", "actor": "job_lead"}
    session._save(state)
    driver.calls.clear()
    original = driver.run

    async def prompt_blocks_javascript(args, **kwargs):
        if driver.dialog_status["hasDialog"] and (args == ["get", "url"] or args[0] == "eval"):
            raise AssertionError("Page JavaScript is blocked by the pending native prompt")
        return await original(args, **kwargs)

    monkeypatch.setattr(driver, "run", prompt_blocks_javascript)
    status = session.ui_action({"action": "control", "owner": "human"})
    assert status["owner"] == "human" and status["dialog"]["message"] == "Pending prompt"
    assert "pending_operation" not in session._read()
    assert ["get", "url"] not in [call[1] for call in driver.calls if call[0] == "run"]
    session.ui_action({"action": "control", "owner": "agent"})
    result, _ = session.dialog({"response": "dismiss"}, actor="job_lead")
    assert result["observation_id"] and driver.dialog_status["hasDialog"] is False


def test_interrupted_coordinate_click_requires_close_without_completing_or_replaying_click(browser):
    session, driver, old = observation(browser)
    state = session._read()
    state["pending_operation"] = {"action": "click_at", "actor": "job_lead"}
    session._save(state)
    driver.calls.clear()
    with pytest.raises(BrowserConflict, match="Close and reopen"):
        session.observe(actor="job_lead", screenshot=False)
    with pytest.raises(BrowserConflict, match="Close and reopen"):
        session.ui_action({"action": "control", "owner": "human"})
    assert commands(driver) == []  # In particular, never synthesize a mouse-up for the old click.
    assert session.status()["owner"] == "agent"
    session.ui_action({"action": "close"})
    assert not driver.alive() and "pending_operation" not in session._read()
    session.ui_action({"action": "start", "mode": "headed"})
    fresh, _ = session.observe(actor="job_lead", screenshot=False)
    assert fresh["session_id"] != old["session_id"] and fresh["observation_id"] != old["observation_id"]
    assert commands(driver) == []


def test_stream_rechecks_session_generation_after_waiting_for_native_frame(browser, monkeypatch):
    from vwb.browser import service

    session, _ = started(browser)
    closed = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)

        def recv(self, **kwargs):
            state = session._read()
            state["session_id"] = "new-session-after-restart"
            session._save(state)
            return json.dumps({"type": "frame", "seq": 8, "data": "old-session-frame"})

    monkeypatch.setattr(service, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(service, "_viewers", {})
    stream = session.stream()
    assert next(stream)[0] == "state"
    with pytest.raises(StopIteration):
        next(stream)
    assert closed == [True] and service._viewers == {}


def test_stream_preserves_new_tab_only_frame_arriving_before_command_state_commit(browser, monkeypatch):
    from vwb.browser import service

    session, _ = started(browser)
    held, release, received, finished = (threading.Event() for _ in range(4))
    second = {"id": "t2", "title": "Static second page", "url": "https://example.test/two", "active": True}
    native = iter([
        {"type": "tabs", "timestamp": (session._read()["tabs_updated_at"] + 1) * 1000,
         "tabs": [{"tabId": "t2", **second}]},
        {"type": "frame", "seq": 9, "data": "only-static-frame"},
    ])
    sent, emitted, errors = [], [], []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def recv(self, **kwargs):
            event = next(native)
            if event["type"] == "frame":
                received.set()
            return json.dumps(event)

        def send(self, value):
            sent.append(json.loads(value))

    monkeypatch.setattr(service, "connect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(service, "_viewers", {})
    stream = session.stream()
    assert next(stream)[0] == "state"

    def command_commit():
        try:
            with session.lock:
                held.set()
                assert release.wait(5), "test did not release the fake tab command"
                state = session._read()
                state.update(tabs=[second], url=second["url"], title=second["title"])
                session._save(state)
        except Exception as exc:
            errors.append(exc)

    def receive():
        try:
            while True:
                event = next(stream)
                emitted.append(event)
                if event[0] == "frame":
                    finished.set()
                    return
        except Exception as exc:
            errors.append(exc)

    command = threading.Thread(target=command_commit)
    consumer = threading.Thread(target=receive)
    command.start()
    try:
        assert held.wait(3)
        consumer.start()
        assert received.wait(3)
        assert not finished.wait(.1)  # New frame waits for the command's state commit.
    finally:
        release.set()
        command.join(timeout=5)
        if consumer.ident is not None:
            consumer.join(timeout=5)
    assert not command.is_alive() and not consumer.is_alive() and not errors
    assert finished.is_set() and emitted[-1][1]["data"] == "only-static-frame"
    assert emitted[-1][1]["tab_id"] == "t2" and sent == []
    assert any(kind == "state" and value["tabs"] == [second] for kind, value in emitted)
    stream.close()
    assert service._viewers == {}


@pytest.fixture
def human_input(browser, monkeypatch):
    session, driver = started(browser)
    session.ui_action({"action": "control", "owner": "human"})
    transcript = []
    original = driver.run

    async def logged(args, **kwargs):
        transcript.append(("native", args))
        return await original(args, **kwargs)

    class Socket:
        def __enter__(self):
            transcript.append(("connect",))
            return self

        def __exit__(self, *_):
            transcript.append(("disconnect",))

        def send(self, value):
            transcript.append(("send", json.loads(value)))

        def ping(self):
            transcript.append(("ping",))
            return SimpleNamespace(wait=lambda timeout: True)

        def recv(self, **kwargs):
            raise ValueError("No incoming messages in this fixture")

        def close(self):
            return None

    monkeypatch.setattr(driver, "run", logged)
    monkeypatch.setattr("vwb.browser.service.connect", lambda *args, **kwargs: Socket())
    return session, driver, transcript


@pytest.mark.parametrize("modifier", [2, 4])
def test_select_all_fixed_operation_preserves_input_batch_order(human_input, modifier):
    from vwb.browser.service import SELECT_ALL

    session, _, transcript = human_input
    pointer = {"type": "input_mouse", "eventType": "mousePressed", "x": 30, "y": 40, "button": "left"}
    down = {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "modifiers": modifier}
    up = dict(down, eventType="keyUp")
    replacement = {"type": "input_keyboard", "eventType": "char", "text": "replacement"}
    session.ui_action({"action": "input", "events": [pointer, down, up, replacement]})
    sent = [row[1] for row in transcript if row[0] == "send"]
    assert [event["eventType"] for event in sent] == ["mousePressed", "keyUp", "char"]
    native = [row[1] for row in transcript if row[0] == "native"]
    assert native[:3] == [["dialog", "status"], ["frame", "main"], ["eval", SELECT_ALL]]
    select_index = transcript.index(("native", ["eval", SELECT_ALL]))
    assert transcript.index(("send", sent[0])) < transcript.index(("ping",)) < select_index
    assert select_index < transcript.index(("send", sent[1])) < transcript.index(("send", sent[2]))
    assert transcript[-2:] == [("ping",), ("disconnect",)]


@pytest.mark.parametrize("modifier", [0, 6, 10])
def test_other_a_key_combinations_remain_native_input(human_input, modifier):
    session, _, transcript = human_input
    event = {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "modifiers": modifier}
    session.ui_action({"action": "input", "events": [event]})
    assert ("send", event) in transcript
    assert not any(row[0] == "native" and row[1][0] == "eval" for row in transcript)


def test_invalid_batch_after_select_all_dispatches_neither_events_nor_fixed_operation(human_input):
    session, driver, transcript = human_input
    driver.calls.clear()
    shortcut = {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "modifiers": 4}
    invalid = {"type": "input_keyboard", "eventType": "char", "text": "x", "modifiers": 32}
    with pytest.raises(ValueError):
        session.ui_action({"action": "input", "events": [shortcut, invalid]})
    assert transcript == [] and driver.calls == []


def test_select_all_does_not_evaluate_page_with_native_dialog_open(human_input):
    session, driver, transcript = human_input
    driver.dialog_status = {"hasDialog": True, "type": "prompt", "message": "Answer first"}
    with pytest.raises(BrowserConflict, match="Resolve the browser dialog"):
        session.ui_action({"action": "input", "events": [
            {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "modifiers": 4},
        ]})
    assert ("ping",) in transcript and ("native", ["dialog", "status"]) in transcript
    assert not any(row[0] == "send" or (row[0] == "native" and row[1][0] == "eval") for row in transcript)


def test_failed_select_all_does_not_send_later_replacement_text(human_input, monkeypatch):
    from vwb.browser.driver import BrowserDriverError
    from vwb.browser.service import SELECT_ALL

    session, driver, transcript = human_input
    original = driver.run

    async def unavailable(args, **kwargs):
        if args == ["eval", SELECT_ALL]:
            raise BrowserDriverError("Select all is unavailable inside this cross-origin frame.")
        return await original(args, **kwargs)

    monkeypatch.setattr(driver, "run", unavailable)
    with pytest.raises(BrowserDriverError, match="cross-origin"):
        session.ui_action({"action": "input", "events": [
            {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "modifiers": 4},
            {"type": "input_keyboard", "eventType": "char", "text": "do not replace a different target"},
        ]})
    assert not any(row[0] == "send" for row in transcript)


def test_agent_select_all_key_chord_uses_fixed_operation_and_returns_fresh_observation(browser):
    from vwb.browser.service import SELECT_ALL

    session, driver, old = observation(browser)
    result, _ = session.act({"action": "press", "key": "Meta+A", "observation_id": old["observation_id"],
                             "screenshot": False}, actor="job_lead")
    assert result["action_completed"] and result["observation_id"] != old["observation_id"]
    assert ["frame", "main"] in commands(driver) and ["press", "Meta+A"] not in commands(driver)
    assert ("run", ["eval", SELECT_ALL]) in driver.calls


@pytest.mark.parametrize("confirm_fence", [True, False])
def test_input_reader_drains_queue_for_pong_and_stops_on_success_or_failure(browser, monkeypatch, confirm_fence):
    session, _ = started(browser)
    session.ui_action({"action": "control", "owner": "human"})

    class Socket:
        def __init__(self):
            self.received = 0
            self.acks = []
            self.inputs = []
            self.reader = None
            self.drained = threading.Event()
            self.closed = threading.Event()
            self.close_count = 0
            self.exited = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.exited = True

        def recv(self, *, timeout):
            self.reader = threading.current_thread()
            if self.received < 24:
                self.received += 1
                return json.dumps({"type": "frame", "seq": self.received, "data": "fixture"})
            if self.closed.wait(timeout):
                raise ValueError("Socket closed")
            raise TimeoutError

        def send(self, value):
            event = json.loads(value)
            if event["type"] == "ack":
                self.acks.append(event["seq"])
                if len(self.acks) == 24:
                    self.drained.set()
            else:
                self.inputs.append(event)

        def ping(self):
            # Model a native receive queue whose pong is unreachable until more
            # than websockets' default 16 messages have been consumed and acked.
            return SimpleNamespace(wait=lambda timeout: self.drained.wait(min(timeout, 1)) and confirm_fence)

        def close(self):
            self.close_count += 1
            self.closed.set()

    socket = Socket()
    monkeypatch.setattr("vwb.browser.service.connect", lambda *args, **kwargs: socket)
    event = {"type": "input_keyboard", "eventType": "char", "text": "typed once", "modifiers": 0}
    if confirm_fence:
        session.ui_action({"action": "input", "events": [event]})
    else:
        with pytest.raises(BrowserConflict, match="delivery was not confirmed"):
            session.ui_action({"action": "input", "events": [event]})
    assert socket.received == 24 and socket.acks == list(range(1, 25))
    assert socket.inputs == [event]
    assert socket.close_count == 1 and socket.closed.is_set() and socket.exited
    assert socket.reader is not None and not socket.reader.is_alive()
