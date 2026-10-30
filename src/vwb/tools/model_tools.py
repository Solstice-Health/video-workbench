"""General model access, with the same policy and provenance path as other tools."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from vwb.gateway.model_access import ModelAccess
from vwb.gateway.model_recommendations import REQUEST_GUIDANCE, freshness, recommendations
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult, schema
from vwb.util.hashing import sha256_file
from vwb.web.workspace_files import open_file
from vwb.workspace.registry import Registry as AssetRegistry

PROVIDER = {"type": "string", "enum": ["openai", "google", "anthropic", "openrouter"]}
CATALOG = schema({"provider": PROVIDER, "query": {"type": "string", "maxLength": 256},
                  "modality": {"type": "string", "maxLength": 64},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                  "offset": {"type": "integer", "minimum": 0, "maximum": 10000}}, ["provider"])
REQUEST = schema({"provider": PROVIDER,
                  "operation": {"type": "string", "minLength": 1, "maxLength": 64},
                  "payload": {"type": "object"},
                  "purpose": {"type": "string", "maxLength": 1000}}, ["provider", "operation", "payload"])


def _access(ctx):
    return ModelAccess(ctx.ws, actor=ctx.agent, deadline=ctx.state.get("model_deadline"),
                       cancel_event=ctx.state.get("cancel_event"))


def model_providers(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if args:
        return ToolResult.error("model_providers takes no arguments.")
    return ToolResult.json({**_access(ctx).providers(), "model_guidance": recommendations()})


def model_catalog(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        Draft202012Validator(CATALOG).validate(args)
    except ValidationError:
        return ToolResult.error("Invalid model_catalog arguments. Use the published schema.")
    result = _access(ctx).catalog(**args)
    items, size = [], 0
    for item in result.get("items", []):
        encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
        if items and size + len(encoded) > 38_000:
            result["next_offset"] = result.get("offset", args.get("offset", 0)) + len(items)
            result["page_size_limited"] = True
            break
        items.append(item)
        size += len(encoded)
    result["items"] = items
    result["model_guidance"] = recommendations(provider=args["provider"])
    return ToolResult.json(result)


def _generation_capabilities(args):
    """Gate known media routes and ambiguous model output before dispatch.

    A native chat/content API can select a model whose default response contains
    media. A missing or text-only modalities field cannot establish that a new
    model honors that restriction. Under a restricted media policy, require its
    gate for these general endpoints too, before any potentially billed call.
    """
    operation, payload = args["operation"], args["payload"]
    caps = set()
    if operation.startswith("images.") or operation == "predict":
        caps.add("gen.image")
    if operation.startswith("video.") or operation == "predict_long_running":
        caps.add("gen.video")
    if operation == "audio.speech":
        caps.add("gen.audio")
    config = payload.get("generationConfig") or payload.get("generation_config") or {}
    modalities = payload.get("modalities", [])
    modalities = modalities if isinstance(modalities, list) else []
    if isinstance(config, dict):
        configured = config.get("responseModalities") or config.get("response_modalities") or []
        if isinstance(configured, list):
            modalities = [*modalities, *configured]
    if isinstance(modalities, list):
        for item in modalities:
            cap = {"image": "gen.image", "video": "gen.video", "audio": "gen.audio"}.get(str(item).lower())
            if cap:
                caps.add(cap)
    if operation in {"chat.completions", "responses", "generate_content"}:
        caps.update(cap for cap in ("gen.image", "gen.video", "gen.audio") if Registry.mode(cap) != "allow")
    return sorted(caps)


def model_request(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        Draft202012Validator(REQUEST).validate(args)
    except ValidationError:
        return ToolResult.error("Invalid model_request arguments. Use the published schema.")
    capabilities = _generation_capabilities(args)

    def guarded(index):
        if index == len(capabilities):
            return _request(ctx, args)
        tool = Tool("model_request", "", REQUEST, lambda _ctx, _args: guarded(index + 1),
                    capabilities[index], "models")
        return Registry([tool]).call(ctx, "model_request", args)

    return guarded(0)


def _measure(path: Path, kind: str) -> tuple[dict, list[bytes]]:
    if kind == "image":
        import numpy as np
        from PIL import Image

        from vwb.gen import qa
        with Image.open(path) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("Generated image exceeds 40 megapixels; inspect it in the sandbox.")
            rgba = np.asarray(image.convert("RGBA"))
        fit = {"native_size": [rgba.shape[1], rgba.shape[0]], "alpha": qa.alpha_stats(rgba)}
        try:
            fit.update(text={"found": qa.read_text(rgba)}, faces=qa.faces(rgba))
        except Exception:
            fit["inspection_note"] = "Automatic text/face inspection unavailable; inspect before using."
        return fit, [path.read_bytes()]
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, timeout=25, check=True)
    data = json.loads(result.stdout)
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == kind), None)
    if stream is None:
        raise ValueError(f"The model output has no {kind} stream.")
    fit = {"seconds": float(stream.get("duration") or data.get("format", {}).get("duration") or 0),
           "codec": stream.get("codec_name")}
    if kind == "video":
        fit["native_size"] = [stream.get("width"), stream.get("height")]
    return fit, []


def _register(ctx, result):
    previews = []
    assets = AssetRegistry(ctx.ws.assets)
    for index, artifact in enumerate(result.get("artifacts", [])):
        mime = artifact.get("mime_type", "")
        kind = mime.split("/", 1)[0]
        if kind not in {"image", "video", "audio"}:
            continue
        relative = artifact["path"].removeprefix("work/")
        if not relative.startswith("model-output/"):
            raise ValueError("The model output is outside its output directory.")
        # The sandbox may edit work/. Snapshot through a pinned descriptor, verify
        # the gateway digest, then register from private host-owned storage.
        private = ctx.ws.root / ".model-access"
        private.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as directory:
            snapshot = Path(directory) / ("output" + Path(relative).suffix)
            with open_file(ctx.ws, "work/" + relative) as source, snapshot.open("wb") as target:
                shutil.copyfileobj(source, target)
            if sha256_file(snapshot) != artifact["sha256"]:
                raise ValueError("Model output changed before it could be registered.")
            try:
                fit, images = _measure(snapshot, kind)
            except Exception as exc:
                artifact["inspection_error"] = f"Output saved but could not be registered as valid {kind}: {type(exc).__name__}"
                continue
            fit.update(provider=result["provider"], model=result.get("model"), op="create",
                       operation=result["operation"], generated=True)
            artifact["asset_ref"] = assets.register(snapshot,
                slot=f"model-{Path(relative).parent.name}-{index}", kind="clip" if kind == "video" else kind,
                receipt="model.generate", derived_from=[{"provider": result["provider"],
                    "operation": result["operation"], "response_path": result.get("response_path"),
                    "inputs": result.get("inputs", [])}], fit=fit)
            if len(previews) < 4:
                previews.extend(images)
    return previews


def _request(ctx, args):
    result = _access(ctx).request(**args)
    images = _register(ctx, result)
    ctx.ledger.append("model.output", ctx.agent, {k: result.get(k) for k in
                      ("provider", "model", "operation", "response_path", "usage", "artifacts")})
    # Large vectors/JSON stay directly readable by scripts in work/. Keeping data
    # and text bounded also protects non-Codex hosts and the script relay.
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 45_000:
        response = json.dumps(result.get("response"), ensure_ascii=False)
        result = {**result, "response": None, "response_truncated": True,
                  "excerpt": response[:8000], "notice": "Read the complete result JSON at response_path; native output is its response field."}
    result["notice"] = (result.get("notice", "") + " Generated media retains model provenance. "
                         "Use asset_ref in canvases; inspect outputs before use. "
                         "A provider key does not guarantee access to every model.").strip()
    # Guidance is advisory. Keep the user's native payload and chosen ID intact,
    # even when the reviewed suggestions have aged out. Never replay generation.
    result["model_guidance"] = freshness()
    return ToolResult.json(result, images=images, artifacts=[a.get("asset_ref") or a["sha256"]
                                                          for a in result.get("artifacts", [])])


TOOLS = [
    Tool("model_providers", "Discover configured model providers, supported operations and dated task-specific model guidance with runtime age and stale-refresh instructions. Host loads .env keys; no credentials are returned. Use this before choosing a model and composing its native request.",
         schema({}), model_providers, "model.call", "models"),
    Tool("model_catalog", "Query live provider model catalogs, optionally filter by name/modality and page with offset. OpenRouter modality=video reads the detailed video-generation catalog; video understanding instead requires video INPUT support. Includes separate dated selection guidance and runtime stale status. Discover current IDs and supported parameters; listings do not guarantee account access. No generation occurs.",
         CATALOG, model_catalog, "model.call", "models"),
    Tool("model_request", "Call a configured OpenAI, Google, Anthropic or OpenRouter model using its native payload and a discovered operation. Supports text/vision, embeddings and model-dependent image/audio/video requests. File inputs use {$file: 'path-under-work', encoding: 'data_url'|'base64'|'text'}; omit encoding for multipart file fields. Optional sha256 pins input bytes and fails before network if changed. Outputs and full JSON save in model-output/; media gets asset_ref and provenance. Poll asynchronous video jobs explicitly. No automatic retry after dispatch. For scripts use sandbox_run(models=true) and Python vwb_models. Provider keys stay on the host." + REQUEST_GUIDANCE,
         REQUEST, model_request, "model.call", "models"),
]
