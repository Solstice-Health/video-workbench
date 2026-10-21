"""Official desktop MCP protocol, tool boundaries and asset imports via fake HTTP."""
from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from vwb.integrations import IntegrationError, IntegrationService
from vwb.integrations.figma_mcp import ENDPOINT, MAX_LOCAL_ASSETS, DesktopFigma
from vwb.integrations.transport import HTTP
from vwb.tools import build_registry
from vwb.tools.base import ToolContext
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request

READ = {"name": "get_design_context", "description": "Read the selected design",
        "inputSchema": {"type": "object", "properties": {"nodeId": {"type": "string"}}, "additionalProperties": False}}


@pytest.fixture
def desktop(tmp_path):
    png = io.BytesIO()
    Image.new("RGB", (8, 8), "orange").save(png, "PNG")
    behavior = {"tools": [READ, {**READ, "name": "get_screenshot"},
                          {**READ, "name": "use_figma", "annotations": {"readOnlyHint": True}}],
                "result": {"content": [{"type": "text", "text": "Design context"}]},
                "asset_mime": "image/png", "asset_bytes": png.getvalue(), "asset_status": 200, "asset_failures": {},
                "unavailable": False}
    calls = []

    def handler(request):
        calls.append(request)
        if behavior["unavailable"]:
            raise httpx.ConnectError("private diagnostic", request=request)
        assert "authorization" not in request.headers
        assert request.url.host in {"127.0.0.1", "localhost"} and request.url.port == 3845
        if request.url.path.startswith("/assets/"):
            status = behavior["asset_failures"].get(request.url.path, behavior["asset_status"])
            if status == "network":
                raise httpx.ReadError("private transport diagnostic", request=request)
            return httpx.Response(status, content=behavior["asset_bytes"],
                                  headers={"Content-Type": behavior["asset_mime"], "Location": "http://127.0.0.1/private"})
        assert str(request.url) == ENDPOINT
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            assert body["params"]["clientInfo"]["name"] == "solstice-video-workbench"
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": behavior["tools"]}
        else:
            assert method == "tools/call"
            result = behavior["result"]
        return httpx.Response(200, headers={"Mcp-Session-Id": "test-figma-session"},
                              json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    service = DesktopFigma(tmp_path / "host", http=HTTP(httpx.Client(transport=httpx.MockTransport(handler))))
    service.behavior, service.calls, service.png = behavior, calls, png.getvalue()
    return service


def workspace(tmp_path):
    ws = Workspace(tmp_path / "r-mcp")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Use the selected Figma design"))
    return ws


def test_connection_requires_explicit_human_action_real_catalog_and_no_credentials(desktop):
    assert not desktop.status()["connected"] and not desktop.calls
    with pytest.raises(IntegrationError, match="Connect desktop MCP"):
        desktop.tools()
    assert not desktop.calls
    status = desktop.connect()
    assert status["connected"] and status["tools_count"] == 2
    assert "client_secret" not in desktop.store.read() and "credentials" not in desktop.store.read()
    tools = desktop.tools(query="screenshot", limit=1)
    assert tools["total"] == 1 and tools["tools"][0]["name"] == "figma_mcp.get_screenshot"
    assert DesktopFigma(desktop.store.root.parent.parent).status()["connected"]
    desktop.disconnect()
    assert not desktop.status()["connected"]
    assert not desktop.store.read().get("tools")


def test_read_context_and_embedded_screenshot_import_via_registry_without_solstice(desktop, tmp_path):
    desktop.connect()
    desktop.behavior["result"] = {"content": [{"type": "image", "data": base64.b64encode(desktop.png).decode(), "mimeType": "image/png"}]}
    service = IntegrationService(tmp_path / "host", desktop=desktop)
    assert not service.status()["items"][0]["connected"]
    assert service.tools(integration="figma_mcp")["total"] == 2
    ws = workspace(tmp_path)
    ctx = ToolContext(ws, "job_lead", "job_lead", None, state={"integration_service": service})
    result = build_registry().call(ctx, "integration_call", {"tool": "figma_mcp.get_screenshot", "arguments": {"nodeId": "1:2"}})
    assert not result.is_error and len(result.images) == 1
    assert result.data["artifacts"][0]["asset_ref"].startswith("slot:import-figma_mcp-")
    assert list((ws.work / "integrations/figma_mcp").glob("*/response.json"))
    assert '"integration": "figma_mcp"' in (ws.ledger / "ledger.jsonl").read_text()
    rpc = [json.loads(r.content) for r in desktop.calls if r.method == "POST"]
    dispatched = [r for r in rpc if r["method"] == "tools/call"]
    assert dispatched[-1]["params"] == {"name": "get_screenshot", "arguments": {"nodeId": "1:2"}}


def test_context_assets_import_without_following_other_local_urls(desktop, tmp_path):
    desktop.connect()
    desktop.behavior["result"] = {"content": [{"type": "text", "text": 'const img="http://localhost:3845/assets/hero.png"; other="http://127.0.0.1:9999/private"; bad="http://localhost:3845/private.png"'}]}
    result = desktop.call(workspace(tmp_path), "figma_mcp.get_design_context", {})
    assert len(result["images"]) == 1
    assert any(row.get("asset_ref") for row in result["artifacts"])
    assets = [r for r in desktop.calls if r.method == "GET"]
    assert len(assets) == 1 and str(assets[0].url) == "http://localhost:3845/assets/hero.png"
    assert "authorization" not in assets[0].headers


@pytest.mark.parametrize("url", ["http://localhost:3845/private.png", "http://127.0.0.1:8765/assets/a.png",
                                "http://user:pass@localhost:3845/assets/a.png", "http://localhost:3845/assets/../secret.png",
                                "http://localhost:3845/assets/%2e%2e.png", "https://evil.example/assets/a.png",
                                "http://localhost:3845/assets/a.png?private=1"])
def test_asset_allowlist_validates_before_network(desktop, url):
    with pytest.raises(IntegrationError, match="Unsupported"):
        desktop._asset(url)
    assert not desktop.calls


def test_mutations_unknown_tools_and_invalid_args_are_blocked(desktop, tmp_path):
    desktop.connect()
    ws = workspace(tmp_path)
    for name, args in [("figma_mcp.use_figma", {}), ("figma_mcp.delete_file", {}),
                       ("figma_mcp.get_design_context", {"unexpected": "argument"})]:
        with pytest.raises(IntegrationError):
            desktop.call(ws, name, args)
    assert all(json.loads(r.content)["method"] != "tools/call" for r in desktop.calls if r.method == "POST")
    desktop.behavior["tools"] = [{**READ, "annotations": {"readOnlyHint": False}}]
    with pytest.raises(IntegrationError, match="unavailable"):
        desktop.refresh()
    assert not desktop.status()["connected"]


def test_stale_connection_and_disconnect_do_not_authorize_calls(desktop, tmp_path):
    desktop.connect()
    desktop.behavior["unavailable"] = True
    with pytest.raises(IntegrationError, match="Open the Figma desktop app"):
        desktop.tools()
    assert desktop.status()["state"] == "error" and not desktop.status()["connected"]
    assert "private diagnostic" not in desktop.status()["message"]
    desktop.disconnect()
    with pytest.raises(IntegrationError, match="Connect desktop MCP"):
        desktop.call(workspace(tmp_path), "figma_mcp.get_design_context", {})


def test_schema_references_and_changed_catalog_are_checked_on_every_call(desktop, tmp_path):
    desktop.connect()
    desktop.behavior["tools"] = [{**READ, "inputSchema": {"$ref": "https://evil.example/schema"}}]
    with pytest.raises(IntegrationError):
        desktop.call(workspace(tmp_path), "figma_mcp.get_design_context", {})
    assert all(json.loads(r.content)["method"] != "tools/call" for r in desktop.calls if r.method == "POST")


def test_asset_redirects_oversize_and_invalid_pixels_fail(desktop, tmp_path):
    desktop.connect()
    desktop.behavior["result"] = {"content": [{"type": "resource_link", "uri": "http://127.0.0.1:3845/assets/hero.png"}]}
    ws = workspace(tmp_path)
    desktop.behavior["asset_status"] = 302
    with pytest.raises(IntegrationError, match="could not be downloaded"):
        desktop.call(ws, "figma_mcp.get_design_context", {})
    desktop.behavior.update(asset_status=200, asset_bytes=b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(IntegrationError, match="exceeds 4 MiB"):
        desktop.call(ws, "figma_mcp.get_design_context", {})
    desktop.behavior["asset_bytes"] = b"not PNG"
    with pytest.raises(IntegrationError, match="invalid"):
        desktop.call(ws, "figma_mcp.get_design_context", {})
    assert not (ws.work / "integrations").exists()


@pytest.mark.parametrize(("asset_count", "content_count"), [(12, 1), (MAX_LOCAL_ASSETS, 136)])
def test_many_local_assets_are_saved_with_only_eight_inline_previews(desktop, tmp_path,
                                                                  asset_count, content_count):
    desktop.connect()
    urls = [f"http://127.0.0.1:3845/assets/image-{index}.png" for index in range(asset_count)]
    content = [{"type": "text", "text": "\n".join(f'const img{index} = "{url}";'
                                                for index, url in enumerate(urls + [urls[0]]))}]
    content.extend({"type": "text", "text": "Context"} for _ in range(content_count - 1))
    desktop.behavior["result"] = {"content": content}
    ws = workspace(tmp_path)
    service = IntegrationService(tmp_path / "host", desktop=desktop)
    ctx = ToolContext(ws, "job_lead", "job_lead", None, state={"integration_service": service})
    result = build_registry().call(ctx, "integration_call",
                                   {"tool": "figma_mcp.get_design_context", "arguments": {}})
    assert not result.is_error, result.text
    assert len(result.images) == 8
    response_path = next((ws.work / "integrations/figma_mcp").glob("*/response.json"))
    saved = json.loads(response_path.read_text())
    assert len(saved["content"]) == content_count + asset_count
    assert len(saved["structuredContent"]["imported_assets"]) == asset_count
    files = list(response_path.parent.glob("artifact-*.png"))
    assert len(files) == asset_count
    assert all(path.read_bytes() == desktop.png for path in files)
    assert len([r for r in desktop.calls if r.method == "GET"]) == asset_count


@pytest.mark.parametrize(("asset_count", "content_count", "message"), [
    (MAX_LOCAL_ASSETS + 1, 1, "more than 64"),
    (2, 199, "too many content items"),
])
def test_local_asset_and_combined_content_caps_fail_before_fetch(desktop, asset_count, content_count, message):
    urls = [f"http://127.0.0.1:3845/assets/image-{index}.png" for index in range(asset_count)]
    content = [{"type": "text", "text": "\n".join(f'const img{index} = "{url}";'
                                                for index, url in enumerate(urls))}]
    content.extend({"type": "text", "text": "Context"} for _ in range(content_count - 1))
    with pytest.raises(IntegrationError, match=message):
        desktop._assets({"content": content})
    assert not desktop.calls


def test_more_asset_slots_do_not_expand_total_download_byte_budget(desktop, tmp_path):
    desktop.connect()
    desktop.behavior["asset_bytes"] = b"x" * (3 * 1024 * 1024)
    desktop.behavior["result"] = {"content": [{
        "type": "text",
        "text": "\n".join(f'const img{i} = "http://127.0.0.1:3845/assets/image-{i}.png";' for i in range(3)),
    }]}
    ws = workspace(tmp_path)
    with pytest.raises(IntegrationError, match="assets exceed 8 MiB"):
        desktop.call(ws, "figma_mcp.get_design_context", {})
    assert len([r for r in desktop.calls if r.method == "GET"]) == 3
    assert not (ws.work / "integrations").exists()


def test_documentation_examples_are_not_downloaded_as_design_assets(desktop, tmp_path):
    desktop.connect()
    real = "http://localhost:3845/assets/actual.png"
    example = "http://localhost:3845/assets/example.png"
    desktop.behavior["asset_failures"] = {"/assets/example.png": 500}
    desktop.behavior["result"] = {"content": [
        {"type": "text", "text": f'const actual = "{real}";\nexport default function Design() {{ return <img src={{actual}}/>; }}'},
        {"type": "text", "text": f'Assets are provided as constants, e.g. const image = "{example}"; use these references.'},
    ]}
    result = desktop.call(workspace(tmp_path), "figma_mcp.get_design_context", {})
    assert not result["isError"]
    assert len(result["images"]) == 1
    assert [str(request.url) for request in desktop.calls if request.method == "GET"] == [real]
    assert example in result["content"][1]["text"]  # Preserve the server's original explanatory context.
    assert result["structuredContent"]["imported_assets"] == [{"source_url": real, "content_index": 2}]


@pytest.mark.parametrize("text", [
    'let img = "http://localhost:3845/assets/actual.png";',
    'var img = "http://localhost:3845/assets/actual.png";',
    '<img src="http://localhost:3845/assets/actual.png" />',
    'background-image: url("http://localhost:3845/assets/actual.png");',
])
def test_source_asset_reference_forms_are_recognized(desktop, text):
    result = desktop._assets({"content": [{"type": "text", "text": text}]})
    assert len(result["structuredContent"]["imported_assets"]) == 1
    assert len(desktop.calls) == 1


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_real_asset_download_failures_remain_hard_failures(desktop, status):
    desktop.behavior["asset_status"] = status
    with pytest.raises(IntegrationError, match="could not be downloaded"):
        desktop._assets({"content": [{"type": "resource_link", "uri": "http://localhost:3845/assets/image.png"}]})
