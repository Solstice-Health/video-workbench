import hashlib
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from vwb.gateway import Gateway
from vwb.tools import build_registry
from vwb.tools import model_tools as tools
from vwb.tools.base import Registry, ToolContext
from vwb.workspace.registry import Registry as AssetRegistry


@pytest.fixture
def ctx(tmp_path):
    ws = SimpleNamespace(root=tmp_path, work=tmp_path / "work", ledger=tmp_path / "ledger", assets=tmp_path / "assets")
    for path in (ws.work, ws.ledger, ws.assets):
        path.mkdir()
    return ToolContext(ws, "model-test", "job_lead", Gateway(ws, actor="model-test"))


@pytest.fixture
def calls(monkeypatch):
    calls = []

    class Fake:
        def providers(self):
            return {"providers": [{"id": "openrouter", "configured": True}]}

        def catalog(self, **args):
            calls.append(("catalog", args))
            return {"items": [{"id": "provider/current-model"}], "next_offset": None}

        def request(self, **args):
            calls.append(("request", args))
            return {**args, "model": args["payload"].get("model"), "response": {"text": "result"},
                    "usage": {"output_tokens": 1}, "artifacts": [], "response_path": "model-output/test/response.json"}

    monkeypatch.setattr(tools, "_access", lambda ctx: Fake())
    return calls


def test_discovery_and_request_registered_for_all_roles(ctx, calls):
    registry = build_registry()
    for role in ("job_lead", "worker", "composer", "scene_miner", "planner_b", "critic"):
        ctx.role = role
        assert {"model_providers", "model_catalog", "model_request"} <= {t.name for t in registry.for_role(role)}
        assert not registry.call(ctx, "model_providers", {}).is_error
    result = registry.call(ctx, "model_request", {"provider": "openrouter", "operation": "chat.completions",
                           "payload": {"model": "vendor/new-model", "messages": [{"role": "user", "content": "Hello"}]}})
    assert not result.is_error
    assert calls[-1][1]["payload"]["model"] == "vendor/new-model"
    assert (ctx.ws.ledger / "tools.jsonl").is_file()


@pytest.mark.parametrize("name,args", [
    ("model_providers", {"key": "not-allowed"}),
    ("model_catalog", {"provider": "unknown"}),
    ("model_catalog", {"provider": "openrouter", "limit": 0}),
    ("model_catalog", {"provider": "openai", "offset": -1}),
    ("model_request", {"provider": "openai", "operation": "responses", "payload": "text"}),
    ("model_request", {"provider": "google", "operation": "generate_content", "payload": {}, "api_key": "no"}),
])
def test_invalid_arguments_never_dispatch(ctx, calls, name, args):
    assert build_registry().call(ctx, name, args).is_error
    assert not calls


@pytest.mark.parametrize("operation,payload,capability", [
    ("images.generate", {"model": "image-model"}, "gen.image"),
    ("video.create", {"model": "video-model"}, "gen.video"),
    ("audio.speech", {"model": "voice-model"}, "gen.audio"),
    ("generate_content", {"generationConfig": {"responseModalities": ["AUDIO"]}}, "gen.audio"),
    ("chat.completions", {"modalities": ["image", "text"]}, "gen.image"),
    ("predict_long_running", {"model": "any"}, "gen.video"),
])
def test_media_policy_cannot_be_skipped(ctx, calls, monkeypatch, operation, payload, capability):
    monkeypatch.setattr(Registry, "mode", staticmethod(lambda cap: "block" if cap == capability else "allow"))
    result = build_registry().call(ctx, "model_request", {"provider": "openrouter", "operation": operation,
                                                          "payload": payload})
    assert result.is_error and capability in result.text
    assert not calls


def test_inherited_permission_applies_before_dispatch(ctx, calls):
    ctx.state["allowed_tools"] = {"model_providers"}
    assert build_registry().call(ctx, "model_catalog", {"provider": "openrouter"}).is_error
    assert not calls


def test_generated_asset_gets_immutable_provenance_and_validated_pixels(ctx, calls, monkeypatch):
    path = ctx.ws.work / "model-output/test/image.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (20, 30), "red").save(path)
    monkeypatch.setattr("vwb.gen.qa.read_text", lambda img: ["sample"])
    monkeypatch.setattr("vwb.gen.qa.faces", lambda img: 0)
    result = {"provider": "openrouter", "operation": "images.generate", "model": "a/model", "response": {},
              "response_path": "model-output/test/response.json", "artifacts": [{"path": "model-output/test/image.png",
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mime_type": "image/png"}]}
    monkeypatch.setattr(tools, "_access", lambda ctx: SimpleNamespace(request=lambda **kw: result))
    value = build_registry().call(ctx, "model_request", {"provider": "openrouter", "operation": "images.generate", "payload": {}})
    assert not value.is_error, value.text
    row = AssetRegistry(ctx.ws.assets).get(value.data["artifacts"][0]["asset_ref"])
    assert row["prov"] == "generated"
    assert row["receipt"] == "model.generate"
    assert json.loads(row["fit"])["native_size"] == [20, 30]
    path.write_bytes(b"overwritten")
    assert Image.open(row["path"]).size == (20, 30)


def test_changed_output_is_not_registered(ctx, calls, monkeypatch):
    path = ctx.ws.work / "model-output/test/image.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (20, 30), "red").save(path)
    result = {"provider": "google", "operation": "generate_content", "model": "x", "response": {}, "artifacts": [
        {"path": "model-output/test/image.png", "sha256": "0" * 64, "mime_type": "image/png"}]}
    monkeypatch.setattr(tools, "_access", lambda ctx: SimpleNamespace(request=lambda **kw: result))
    value = build_registry().call(ctx, "model_request", {"provider": "google", "operation": "generate_content", "payload": {}})
    assert value.is_error and "changed" in value.text
    assert not AssetRegistry(ctx.ws.assets).list()


def test_large_vectors_do_not_flood_agent_or_bridge(ctx, calls, monkeypatch):
    response = {"provider": "openai", "operation": "embeddings", "model": "x", "artifacts": [],
                "response": {"data": [0.12345678] * 20000}, "response_path": "model-output/large/response.json"}
    monkeypatch.setattr(tools, "_access", lambda ctx: SimpleNamespace(request=lambda **kw: response))
    result = build_registry().call(ctx, "model_request", {"provider": "openai", "operation": "embeddings", "payload": {}})
    assert not result.is_error
    assert result.data["response_truncated"]
    assert result.data["response"] is None
    assert len(result.text) < 12000
