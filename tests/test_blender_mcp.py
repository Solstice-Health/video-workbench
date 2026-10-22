"""Real SDK stdio handshake, tool permissions and artifact imports, without Blender."""

from __future__ import annotations

import ast
import base64
import io
import json
import sys
from pathlib import Path

import pytest
from mcp.client.stdio import StdioServerParameters
from PIL import Image

from vwb.integrations.blender_mcp import CODE_TOOL, LOCK, PREFIX, DesktopBlender
from vwb.integrations.store import IntegrationError
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request

SCHEMA = {"type": "object", "properties": {"limit": {"type": "integer"}}, "additionalProperties": False}
CODE_SCHEMA = {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"],
               "additionalProperties": False}
CATALOG = [{"name": name, "description": "A local Blender tool", "inputSchema": SCHEMA}
           for name in ("get_scene_info", "look", "search_mentions", "get_addon_status", "generate_3d", "import_asset")]
CATALOG.append({"name": CODE_TOOL, "description": "Run local Blender Python", "inputSchema": CODE_SCHEMA})
CATALOG[1]["inputSchema"] = {"type": "object", "properties": {"image": {"type": "string"}}, "additionalProperties": False}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "orange").save(image, "PNG")
    state = tmp_path / "peer-state.json"
    peer = tmp_path / "peer.py"
    calls = tmp_path / "peer-calls.jsonl"
    state.write_text(json.dumps({"catalog": CATALOG, "image": base64.b64encode(image.getvalue()).decode()}))
    peer.write_text('''import sys,json,os
from pathlib import Path
state=Path(sys.argv[1]); calls=Path(sys.argv[2])
for line in sys.stdin:
    request=json.loads(line)
    with calls.open('a') as log: log.write(json.dumps(request)+'\\n')
    if 'id' not in request: continue
    data=json.loads(state.read_text()); method=request['method']
    if method=='initialize':
        result={'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif method=='tools/list': result={'tools':data['catalog']}
    elif method=='tools/call':
        tool=request['params']['name']
        if data.get('fail'): result={'content':[{'type':'text','text':'Error getting scene info: private detail'}]}
        elif tool=='look':
            image=request['params'].get('arguments',{}).get('image')
            if image and image.startswith('/'):
                assert Path(image).is_file()
                assert Path(image).read_bytes()==__import__('base64').b64decode(data['image'])
            result={'content':[{'type':'image','mimeType':'image/png','data':data['image']}]}
        elif tool=='execute_blender_code' and 'VWB_IMAGE_EXISTS:' in request['params'].get('arguments',{}).get('code',''):
            result={'content':[{'type':'text','text':'Code executed successfully: VWB_IMAGE_EXISTS:'+('no' if data.get('missing_image') else 'yes')}]}
        else: result={'content':[{'type':'text','text':json.dumps({'tool':tool,'arguments':request['params'].get('arguments'), 'env':dict(os.environ)})}]}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result},ensure_ascii=False),flush=True)
''')
    desktop = DesktopBlender(tmp_path / "host")
    monkeypatch.setattr(desktop, "_prepared", lambda: True)

    def parameters(saved):
        return StdioServerParameters(command=sys.executable, args=["-I", str(peer), str(state), str(calls)],
                                     env=desktop._environment(saved.get("port", 9876)), cwd=tmp_path)

    monkeypatch.setattr(desktop, "_parameters", parameters)
    desktop.peer_state, desktop.peer_calls, desktop.png = state, calls, image.getvalue()
    return desktop


def workspace(tmp_path):
    ws = Workspace(tmp_path / "r-blender")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True, exist_ok=True)
    ws.save(Request(id=ws.id, message="Build a Blender scene"))
    return ws


def read_calls(bridge):
    return [json.loads(line) for line in bridge.peer_calls.read_text().splitlines()]


def test_handshake_and_probe_are_real_and_cloud_tools_are_never_exposed(bridge):
    assert not bridge.status()["connected"]
    status = bridge.connect()
    assert status["connected"] and status["tools_count"] == 4
    names = {row["name"] for row in bridge.tools()["tools"]}
    assert names == {PREFIX + name for name in ("get_scene_info", "look", "search_mentions", "get_addon_status")}
    requests = read_calls(bridge)
    assert [r["method"] for r in requests[:4]] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert requests[-1]["params"] == {"name": "get_scene_info", "arguments": {"limit": 1}}


def test_no_secrets_or_parent_python_environment_reach_stdio_bridge(bridge, tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "AWS_SESSION_TOKEN", "PYTHONPATH"):
        monkeypatch.setenv(name, "test-super-secret")
    monkeypatch.setenv("HTTP_PROXY", "https://test-super-secret.example")
    bridge.connect()
    result = bridge.call(workspace(tmp_path), PREFIX + "get_scene_info", {})
    env = json.loads(result["content"][0]["text"])["env"]
    assert "test-super-secret" not in json.dumps(env)
    assert env["BLENDER_HOST"] == "127.0.0.1" and env["BLENDER_PORT"] == "9876"
    assert env["DISABLE_TELEMETRY"] == "true"
    assert env["HOME"] == str(bridge.runtime / "home")


def test_viewport_image_imports_as_registered_workspace_asset(bridge, tmp_path):
    bridge.connect()
    ws = workspace(tmp_path)
    result = bridge.call(ws, PREFIX + "look", {})
    assert result["images"] == [bridge.png]
    assert result["artifacts"][0]["asset_ref"].startswith("slot:import-blender_mcp-")
    assert list((ws.work / "integrations/blender_mcp").glob("*/response.json"))
    assert '"integration": "blender_mcp"' in (ws.ledger / "ledger.jsonl").read_text()


def test_python_needs_explicit_permission_and_execution_dispatch(bridge, tmp_path):
    bridge.connect()
    ws = workspace(tmp_path)
    with pytest.raises(IntegrationError, match="Enable Blender Python"):
        bridge.call(ws, PREFIX + CODE_TOOL, {"code": "print(1)"}, read_only=False)
    bridge.configure(allow_code_execution=True)
    assert PREFIX + CODE_TOOL in {r["name"] for r in bridge.tools()["tools"]}
    assert bridge.status()["tools_count"] == 5
    with pytest.raises(IntegrationError, match="Enable Blender Python"):
        bridge.call(ws, PREFIX + CODE_TOOL, {"code": "print(1)"})
    result = bridge.call(ws, PREFIX + CODE_TOOL, {"code": "print(1)"}, read_only=False)
    assert json.loads(result["content"][0]["text"])["arguments"] == {"code": "print(1)"}
    bridge.configure(allow_code_execution=False)
    assert PREFIX + CODE_TOOL not in {r["name"] for r in bridge.tools()["tools"]}


@pytest.mark.parametrize("name", ["generate_3d", "import_asset", "search_assets", "unknown", "execute_code"])
def test_unapproved_tools_block_before_starting_process(bridge, tmp_path, name):
    bridge.connect()
    bridge.configure(allow_code_execution=True)
    count = len(read_calls(bridge))
    with pytest.raises(IntegrationError, match="does not expose"):
        bridge.call(workspace(tmp_path), PREFIX + name, {}, read_only=False)
    assert len(read_calls(bridge)) == count


def test_schema_rejection_precedes_remote_dispatch(bridge, tmp_path):
    bridge.connect()
    count = sum(r["method"] == "tools/call" for r in read_calls(bridge))
    with pytest.raises(IntegrationError, match="schema"):
        bridge.call(workspace(tmp_path), PREFIX + "get_scene_info", {"limit": "bad"})
    assert sum(r["method"] == "tools/call" for r in read_calls(bridge)) == count


def test_no_connect_claim_when_only_mcp_process_is_alive(bridge):
    value = json.loads(bridge.peer_state.read_text())
    value["fail"] = True
    bridge.peer_state.write_text(json.dumps(value))
    with pytest.raises(IntegrationError, match="addon did not answer"):
        bridge.connect()
    assert not bridge.status()["connected"] and bridge.status()["state"] == "error"


def test_catalog_schemas_cannot_fetch_remote_references(bridge):
    value = json.loads(bridge.peer_state.read_text())
    value["catalog"][0]["inputSchema"] = {"$ref": "https://private.example/schema"}
    bridge.peer_state.write_text(json.dumps(value))
    with pytest.raises(IntegrationError, match="MCP operation|local definitions"):
        bridge.connect()


def test_disconnect_and_port_change_invalidate_prior_verification(bridge, tmp_path):
    bridge.connect()
    bridge.configure(port=9877)
    assert not bridge.status()["connected"]
    with pytest.raises(IntegrationError, match="Connect Blender"):
        bridge.tools()
    bridge.connect()
    assert bridge.status()["port"] == 9877
    bridge.disconnect()
    assert not bridge.status()["connected"]
    with pytest.raises(IntegrationError, match="Connect Blender"):
        bridge.call(workspace(tmp_path), PREFIX + "look", {})


@pytest.mark.parametrize("kwargs", [{"port": 22}, {"port": True}, {"port": 65536}, {"allow_code_execution": "yes"}])
def test_only_fixed_loopback_valid_port_and_explicit_boolean_permission(bridge, kwargs):
    with pytest.raises(IntegrationError, match="explicit Python permission"):
        bridge.configure(**kwargs)


def test_prepare_is_explicit_and_no_unprepared_process_can_spawn(tmp_path):
    bridge = DesktopBlender(tmp_path / "host")
    assert not bridge.status()["prepared"]
    assert bridge.status()["addon_path"] is None
    with pytest.raises(IntegrationError, match="Prepare the pinned"):
        bridge.connect()
    assert not bridge.python.exists()


def test_runtime_lock_pins_bridge_and_every_dependency_hash():
    text = LOCK.read_text()
    assert "mcp-for-blender==2.2.2" in text and "mcp==1.26.0" in text
    rows = [row for row in text.splitlines() if row and not row.startswith(" ")]
    assert len(rows) >= 20
    assert all("==" in row and row.endswith("\\") for row in rows)
    assert "7dfaadb509b0cc8902367a0b327f8ed4df1c06a8721bf0c1aba2a96c82717e0d" in text


def test_runtime_storage_symlinks_rejected(tmp_path):
    bridge = DesktopBlender(tmp_path / "host")
    outside = tmp_path / "outside"
    outside.mkdir()
    (bridge.store.root / "runtime").symlink_to(outside, target_is_directory=True)
    assert not bridge.status()["prepared"]
    with pytest.raises(IntegrationError, match="symlinks"):
        bridge.prepare()


def test_partial_configuration_preserves_custom_port_and_python_permission(bridge):
    bridge.configure(port=9988, allow_code_execution=True)
    bridge.configure(allow_code_execution=False)
    assert bridge.status()["port"] == 9988
    bridge.configure(allow_code_execution=True)
    bridge.configure(port=9989)
    assert bridge.status()["allow_code_execution"] is True
    bridge.configure()
    assert bridge.status()["port"] == 9989


@pytest.mark.parametrize("path", ["/private/tmp/private.png", "../secret.png", "~/secret.png", "file:///tmp/private.png"])
def test_look_cannot_read_outside_workspace_files(bridge, tmp_path, path):
    bridge.connect()
    count = len(read_calls(bridge))
    with pytest.raises(IntegrationError, match="workspace|work folder|traversal"):
        bridge.call(workspace(tmp_path), PREFIX + "look", {"image": path})
    assert len(read_calls(bridge)) == count


@pytest.mark.parametrize("style", ["relative", "absolute", "container"])
def test_look_pins_workspace_image_and_removes_private_snapshot(bridge, tmp_path, style):
    bridge.connect()
    ws = workspace(tmp_path)
    image = ws.work / "renders/source.png"
    image.parent.mkdir()
    image.write_bytes(bridge.png)
    value = {"relative": "renders/source.png", "absolute": str(image), "container": "/work/renders/source.png"}[style]
    result = bridge.call(ws, PREFIX + "look", {"image": value})
    assert result["images"] == [bridge.png]
    assert result["arguments"]["image"] == value
    dispatched = [row for row in read_calls(bridge) if row.get("params", {}).get("name") == "look"][-1]
    private_path = Path(dispatched["params"]["arguments"]["image"])
    assert private_path.is_relative_to(bridge.runtime / "tmp")
    assert not private_path.exists()


def test_look_rejects_workspace_symlink_before_dispatch(bridge, tmp_path):
    bridge.connect()
    ws = workspace(tmp_path)
    image = tmp_path / "outside.png"
    image.write_bytes(bridge.png)
    (ws.work / "images").symlink_to(tmp_path, target_is_directory=True)
    count = len(read_calls(bridge))
    with pytest.raises(IntegrationError, match="Symlinks"):
        bridge.call(ws, PREFIX + "look", {"image": "images/outside.png"})
    assert len(read_calls(bridge)) == count


@pytest.mark.parametrize("name", ["Render Result", "texture.png", "x');__import__('os').system('pwd');#"])
def test_loaded_image_name_requires_fixed_injection_safe_read_probe(bridge, tmp_path, name):
    bridge.connect()
    assert bridge.status()["allow_code_execution"] is False
    result = bridge.call(workspace(tmp_path), PREFIX + "look", {"image": name})
    assert result["images"]
    calls = [row for row in read_calls(bridge) if row.get("params", {}).get("name") == CODE_TOOL]
    code = calls[-1]["params"]["arguments"]["code"]
    tree = ast.parse(code)
    get = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == "get")
    assert get.args[0].value == name
    assert not any(isinstance(node, ast.Name) and node.id == "__import__" for node in ast.walk(tree))


def test_missing_native_image_name_cannot_fall_back_to_bridge_cwd(bridge, tmp_path):
    bridge.connect()
    value = json.loads(bridge.peer_state.read_text())
    value["missing_image"] = True
    bridge.peer_state.write_text(json.dumps(value))
    with pytest.raises(IntegrationError, match="not present in Blender"):
        bridge.call(workspace(tmp_path), PREFIX + "look", {"image": "private.png"})
    assert not any(row.get("params", {}).get("name") == "look" for row in read_calls(bridge))
