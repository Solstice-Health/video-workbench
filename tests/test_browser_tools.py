"""Browser calls through the real registry, without a browser or model service."""

from __future__ import annotations

import io
import json
from copy import deepcopy

import pytest
from PIL import Image

from vwb.config import load_config
from vwb.tools import browser_tools, build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


@pytest.fixture
def context(tmp_path):
    ws = Workspace(tmp_path / "r-browser-tools")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.root.name, message="Inspect a page"))
    return ToolContext(ws=ws, agent="job_lead", role="job_lead", gateway=None)


@pytest.fixture
def fake_service(monkeypatch):
    calls = []
    output = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(output, format="PNG")

    class Session:
        def __init__(self, ws):
            calls.append(("construct", ws.id))

        def open(self, **kwargs):
            calls.append(("open", kwargs))
            return {"observation_id": "observation-1", "snapshot": "A page"}, [output.getvalue()]

        def observe(self, **kwargs):
            calls.append(("observe", kwargs))
            return {"observation_id": "observation-2"}, []

        def act(self, args, **kwargs):
            calls.append(("act", args, kwargs))
            return {"observation_id": "observation-3", "download": {"sha256": "a" * 64}}, []

    monkeypatch.setattr(browser_tools, "BrowserSession", Session)
    return calls


def test_browser_tools_registered_only_for_current_authorized_role(context, fake_service):
    registry = build_registry()
    expected = {tool.name for tool in browser_tools.TOOLS}
    assert expected == {"browser_open", "browser_observe", "browser_act", "browser_files", "browser_dialog", "browser_close"}
    assert expected <= {tool.name for tool in registry.for_role("job_lead")}
    for role in ("scene_miner", "planner_b", "composer", "critic", "unknown"):
        assert not expected.intersection(tool.name for tool in registry.for_role(role))
        context.role = role
        result = registry.call(context, "browser_open", {})
        assert result.is_error and "may not call" in result.text
    assert fake_service == []


def test_registry_bounds_browser_screenshot_and_records_hashes(context, fake_service):
    result = build_registry().call(context, "browser_open", {"url": "https://example.test/"})
    assert not result.is_error and result.data["observation_id"] == "observation-1"
    assert len(result.images) == 1
    image = Image.open(io.BytesIO(result.images[0]))
    assert image.size == (1568, 1045) and len(result.images[0]) <= 600_000
    assert fake_service[-1] == ("open", {"actor": "job_lead", "url": "https://example.test/", "mode": "headed"})
    row = json.loads((context.ws.ledger / "tools.jsonl").read_text().splitlines()[-1])
    assert row["tool"] == "browser_open" and row["ok"]
    assert len(row["input_sha256"]) == len(row["output_sha256"]) == 64


@pytest.mark.parametrize("action", ["upload", "download"])
def test_file_policy_cannot_be_bypassed_through_general_browser_action(context, fake_service, monkeypatch, action):
    policy = deepcopy(load_config("policy"))
    policy["capabilities"]["browser.files"] = "block"
    policy["capabilities"]["browser.control"] = "allow"
    monkeypatch.setattr("vwb.tools.base.load_config", lambda name: policy)
    registry = build_registry()
    arguments = {"observation_id": "observation-1", "action": action, "ref": "e1", "path": "work/report.txt"}
    blocked = registry.call(context, "browser_files", arguments)
    assert blocked.is_error and "browser.files: block" in blocked.text
    smuggled = registry.call(context, "browser_act", arguments)
    assert smuggled.is_error and "dedicated file" in smuggled.text
    assert fake_service == []


@pytest.mark.parametrize(("capability", "tool"), [("browser.observe", "browser_observe"), ("browser.control", "browser_open")])
def test_browser_policy_is_checked_before_service_construction(context, fake_service, monkeypatch, capability, tool):
    policy = deepcopy(load_config("policy"))
    policy["capabilities"][capability] = "block"
    monkeypatch.setattr("vwb.tools.base.load_config", lambda name: policy)
    result = build_registry().call(context, tool, {})
    assert result.is_error and "blocked by the policy dial" in result.text
    assert fake_service == []


def test_successful_file_tool_retains_download_artifact_hash(context, fake_service):
    arguments = {"observation_id": "observation-1", "action": "download", "ref": "e1", "filename": "report.pdf"}
    result = build_registry().call(context, "browser_files", arguments)
    assert not result.is_error and result.artifacts == ["a" * 64]
    assert fake_service[-1] == ("act", arguments, {"actor": "job_lead"})


def test_unconfigured_remote_browser_fails_without_launch_or_fallback(context, monkeypatch):
    from vwb.browser.driver import AgentBrowserDriver

    monkeypatch.delenv("VWB_BROWSER_CDP_URL", raising=False)
    calls = []
    monkeypatch.setattr("vwb.browser.runtime.socket_directory", lambda private, session: context.ws.root / "test-socket")

    async def forbidden_runner(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Unconfigured remote mode must not execute a browser command")

    driver = AgentBrowserDriver(context.ws.root / ".browser", "test-remote", runner=forbidden_runner)
    monkeypatch.setattr("vwb.browser.service.AgentBrowserDriver", lambda *args, **kwargs: driver)
    result = build_registry().call(context, "browser_open", {"mode": "remote"})
    assert result.is_error and "VWB_BROWSER_CDP_URL" in result.text
    assert calls == [] and not driver.config_path.exists()
    state = json.loads((context.ws.root / ".browser" / "state.json").read_text())
    assert state["state"] == "error" and "could not start" in state["error"]
