"""Native model routing and the workspace/credential boundary, without paid calls."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
import wave
from types import SimpleNamespace

import httpx
import pytest

from vwb.gateway import model_access as module
from vwb.gateway.model_access import ModelAccess, ModelAccessError
from vwb.workspace import Workspace

SECRET = "private-host-key-do-not-return"


@pytest.fixture
def access(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "request")
    ws.work.mkdir(parents=True)
    ws.ledger.mkdir()
    monkeypatch.setattr(module, "secrets", lambda: SimpleNamespace(require=lambda provider: SECRET))
    monkeypatch.setattr(module, "provider_status", lambda: [
        {"id": p, "label": p, "configured": True, "source": ".env"} for p in module.BASES
    ])
    return ModelAccess(ws, "test-agent")


@pytest.fixture
def intercept(monkeypatch):
    original = httpx.Client
    calls = []

    def install(handler):
        def wrapped(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: original(
            **kwargs, transport=httpx.MockTransport(wrapped)))
        return calls
    return install


def reply(data, code=200):
    return httpx.Response(code, json=data)


def test_provider_help_reports_operations_without_keys(access):
    result = access.providers()
    assert {p["id"] for p in result["items"]} == set(module.BASES)
    router = next(p for p in result["items"] if p["id"] == "openrouter")
    assert {"images.generate", "audio.speech", "audio.transcribe", "video.create", "embeddings"} <= {
        op["name"] for op in router["operations"]}
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(("modality", "path"), [("video", "/api/v1/videos/models"),
                                               ("image", "/api/v1/images/models"),
                                               ("embeddings", "/api/v1/models")])
def test_openrouter_catalog_uses_modality_specific_routes(access, intercept, modality, path):
    calls = intercept(lambda request: reply({"data": [
        {"id": "new-model/a", "name": "A", "architecture": {"output_modalities": [modality]}},
        {"id": "new-model/b", "name": "B", "pricing": {"prompt": "0.001"}},
        {"id": "new-model/c", "name": "C"},
    ]}))
    result = access.catalog("openrouter", query="model", modality=modality, limit=1, offset=1)
    assert calls[0].url.path == path
    assert calls[0].headers["authorization"] == f"Bearer {SECRET}"
    assert result["items"] == [{"id": "new-model/b", "name": "B", "pricing": {"prompt": "0.001"}}]
    assert result["next_offset"] == 2
    assert result["total_matching"] == 3
    if modality == "embeddings":
        assert calls[0].url.params["output_modalities"] == "embeddings"


def test_openai_catalog_does_not_guess_modalities_from_ids(access, intercept):
    intercept(lambda request: reply({"data": [{"id": "brand-new-unknown-model"}]}))
    result = access.catalog("openai", modality="video")
    assert result["items"] == [{"id": "brand-new-unknown-model"}]
    assert result["modality_metadata"] == "not_advertised"
    assert result["modality_filter_applied"] is False


def test_model_catalog_preserves_native_capability_fields(access, intercept):
    row = {"id": "new/video", "supported_durations": [5, 10], "supported_resolutions": ["720p"],
           "allowed_passthrough_parameters": ["generate_audio"], "max_reference_images": 5,
           "future_capability": {"enabled": True}}
    intercept(lambda request: reply({"data": [row]}))
    assert access.catalog("openrouter", modality="video")["items"] == [row]


def test_google_catalog_pagination_and_advertised_embedding_methods(access, intercept):
    def handler(request):
        if request.url.params.get("pageToken") == "next-page":
            return reply({"models": [{"name": "models/embed-new", "supportedGenerationMethods": ["embedContent"]}]})
        return reply({"models": [{"name": "models/unknown", "supportedGenerationMethods": ["generateContent"]}],
                      "nextPageToken": "next-page"})
    calls = intercept(handler)
    result = access.catalog("google", modality="embedding")
    assert len(calls) == 2
    assert result["items"][0]["name"] == "models/embed-new"
    assert result["provider_catalog_truncated"] is False
    assert calls[0].headers["x-goog-api-key"] == SECRET
    assert "key=" not in str(calls[0].url)


def test_anthropic_catalog_cursor_and_version(access, intercept):
    def handler(request):
        if request.url.params.get("after_id"):
            return reply({"data": [{"id": "new-second"}], "has_more": False})
        return reply({"data": [{"id": "new-first"}], "has_more": True, "last_id": "new-first"})
    calls = intercept(handler)
    result = access.catalog("anthropic", limit=1)
    assert result["next_offset"] == 1
    assert calls[1].url.params["after_id"] == "new-first"
    assert calls[0].headers["anthropic-version"] == "2023-06-01"


def test_native_embedding_response_saved_and_usage_logged(access, intercept):
    calls = intercept(lambda request: reply({"model": "new/embedding", "data": [{"embedding": [0.1, 0.2]}],
                                           "usage": {"prompt_tokens": 7, "total_tokens": 7}}))
    result = access.request("openrouter", "embeddings", {"model": "new/embedding", "input": "hello"}, "Index new assets")
    assert calls[0].url == "https://openrouter.ai/api/v1/embeddings"
    assert result["response"]["data"][0]["embedding"] == [0.1, 0.2]
    assert result["usage"]["input_tokens"] == 7
    assert result["artifacts"] == []
    saved = json.loads((access.ws.work / result["response_path"]).read_text())
    assert saved["response"] == result["response"]
    ledger = (access.ws.ledger / "tools.jsonl").read_text()
    assert "test-agent" in ledger and '"ok": true' in ledger
    assert SECRET not in ledger


def test_nested_file_markers_expand_without_entering_history(access, intercept):
    (access.ws.work / "photo.png").write_bytes(b"png-bytes")
    (access.ws.work / "notes.txt").write_text("small note")
    calls = intercept(lambda request: reply({"choices": [{"message": {"content": "seen"}}]}))
    payload = {"model": "new/image-model", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": {"$file": "photo.png", "encoding": "data_url"}}},
        {"type": "text", "text": {"$file": "notes.txt", "encoding": "text"}},
    ]}]}
    access.request("openrouter", "chat.completions", payload)
    sent = json.loads(calls[0].content)
    assert sent["messages"][0]["content"][0]["image_url"]["url"] == "data:image/png;base64,cG5nLWJ5dGVz"
    assert sent["messages"][0]["content"][1]["text"] == "small note"
    assert payload["messages"][0]["content"][0]["image_url"]["url"]["$file"] == "photo.png"
    log = (access.ws.ledger / "tools.jsonl").read_text()
    assert "cG5nLWJ5dGVz" not in log


@pytest.mark.parametrize(("provider", "operation", "payload", "path"), [
    ("openai", "audio.transcribe", {"model": "new-transcribe", "file": {"$file": "sound.mp3"},
                                   "timestamp_granularities[]": ["word", "segment"]}, "/v1/audio/transcriptions"),
    ("openrouter", "audio.transcribe", {"model": "new/transcribe", "file": {"$file": "sound.mp3"}}, "/api/v1/audio/transcriptions"),
    ("openai", "video.create", {"model": "new-video", "prompt": "move", "input_reference": {"$file": "sound.mp3"}, "seconds": "4"}, "/v1/videos"),
])
def test_multipart_operations_relay_binary_and_fields(access, intercept, provider, operation, payload, path):
    (access.ws.work / "sound.mp3").write_bytes(b"audio-file-bytes")
    calls = intercept(lambda request: reply({"id": "job1", "text": "transcribed"}))
    access.request(provider, operation, payload)
    assert calls[0].url.path == path
    assert calls[0].headers["content-type"].startswith("multipart/form-data;")
    assert b"audio-file-bytes" in calls[0].content
    assert b'filename="sound.mp3"' in calls[0].content
    assert b'$file' not in calls[0].content


def test_multipart_mime_override_and_immutable_input_digest(access, intercept):
    import hashlib
    (access.ws.work / "audio.bin").write_bytes(b"first audio")
    calls = intercept(lambda request: reply({"text": "hello"}))
    payload = {"model": "new", "file": {"$file": "audio.bin", "mime_type": "audio/wav"}}
    first = access.request("openai", "audio.transcribe", payload)
    assert b"Content-Type: audio/wav" in calls[0].content
    assert first["inputs"] == [{"path": "audio.bin", "mime_type": "audio/wav", "bytes": 11,
                                "sha256": hashlib.sha256(b"first audio").hexdigest()}]
    (access.ws.work / "audio.bin").write_bytes(b"changed audio")
    second = access.request("openai", "audio.transcribe", payload)
    assert first["inputs"][0]["sha256"] != second["inputs"][0]["sha256"]
    records = [json.loads(line) for line in (access.ws.ledger / "tools.jsonl").read_text().splitlines()]
    assert records[0]["input_sha256"] != records[1]["input_sha256"]


def test_multipart_invalid_mime_override_rejected(access):
    (access.ws.work / "audio.bin").write_bytes(b"x")
    with pytest.raises(ValueError, match="MIME"):
        access.request("openai", "audio.transcribe", {"model": "new", "file": {"$file": "audio.bin", "mime_type": "audio/wav\r\nx-key: danger"}})


def test_image_edit_multiple_files_and_mask(access, intercept):
    (access.ws.work / "a.png").write_bytes(b"image-a")
    (access.ws.work / "b.png").write_bytes(b"image-b")
    calls = intercept(lambda request: reply({"data": [{"b64_json": base64.b64encode(b"image-result").decode()}]}))
    result = access.request("openai", "images.edit", {
        "model": "new-image", "image": [{"$file": "a.png"}, {"$file": "b.png"}],
        "mask": {"$file": "a.png"}, "prompt": "combine"})
    assert calls[0].content.count(b'name="image[]"') == 2
    assert b'name="mask"' in calls[0].content
    artifact = result["artifacts"][0]
    assert artifact["mime_type"] == "image/png"
    assert (access.ws.work / artifact["path"]).read_bytes() == b"image-result"
    assert result["response"]["data"][0]["b64_json"] == {"artifact": artifact["path"]}


def test_google_native_image_audio_and_usage_saved(access, intercept):
    pcm = b"\x00\x00\x01\x00" * 24
    def handler(request):
        assert request.url.path == "/v1beta/models/new-tts:generateContent"
        assert "model" not in json.loads(request.content)
        return reply({"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(pcm).decode()}},
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"png").decode()}},
        ]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 12}})
    intercept(handler)
    result = access.request("google", "generate_content", {"model": "models/new-tts", "contents": [{"parts": [{"text": "Hello"}]}]})
    assert len(result["artifacts"]) == 2
    assert result["usage"]["input_tokens"] == 5
    audio = result["artifacts"][0]
    assert audio["mime_type"] == "audio/wav"
    with wave.open(io.BytesIO((access.ws.work / audio["path"]).read_bytes())) as handle:
        assert handle.getframerate() == 24000
        assert handle.readframes(1000) == pcm


def test_google_embed_keeps_native_model_field(access, intercept):
    calls = intercept(lambda request: reply({"embedding": {"values": [1, 2]}}))
    access.request("google", "embed_content", {"model": "new-embed", "content": {"parts": [{"text": "a"}]}})
    assert json.loads(calls[0].content)["model"] == "models/new-embed"
    assert calls[0].url.path.endswith("/models/new-embed:embedContent")


def test_openrouter_transcription_base64_json(access, intercept):
    (access.ws.work / "voice.wav").write_bytes(b"wave")
    calls = intercept(lambda request: reply({"text": "heard"}))
    result = access.request("openrouter", "audio.transcribe", {
        "model": "new/transcribe", "input_audio": {"data": {"$file": "voice.wav", "encoding": "base64"}, "format": "wav"}})
    assert result["response"]["text"] == "heard"
    assert json.loads(calls[0].content)["input_audio"]["data"] == "d2F2ZQ=="


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_speech_returns_file_instead_of_binary_in_history(access, intercept, provider):
    intercept(lambda request: httpx.Response(200, content=b"audio-bytes", headers={"content-type": "audio/mpeg"}))
    result = access.request(provider, "audio.speech", {"model": "new-voice", "input": "hello", "voice": "supported", "response_format": "mp3"})
    assert result["artifacts"][0]["path"].endswith(".mp3")
    assert result["response"]["artifact"] == result["artifacts"][0]["path"]
    assert b"audio-bytes" == (access.ws.work / result["artifacts"][0]["path"]).read_bytes()


def test_openrouter_chat_inline_image_data_url_saved(access, intercept):
    intercept(lambda request: reply({"choices": [{"message": {"images": [{"image_url": {
        "url": "data:image/webp;base64," + base64.b64encode(b"webp-output").decode()}}]}}]}))
    result = access.request("openrouter", "chat.completions", {"model": "new/image", "messages": [], "modalities": ["text", "image"]})
    assert result["artifacts"][0]["path"].endswith(".webp")
    assert "d2VicC1vdXRwdXQ=" not in json.dumps(result)


def test_async_video_uses_fixed_poll_and_content_routes(access, intercept):
    def handler(request):
        if request.method == "POST":
            return reply({"id": "job-1", "polling_url": "https://malicious.example/steal", "status": "pending"}, 202)
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"video", headers={"content-type": "video/mp4"})
        return reply({"id": "job-1", "status": "completed", "usage": {"cost": 0.01}})
    calls = intercept(handler)
    access.request("openrouter", "video.create", {"model": "new/video", "prompt": "x"})
    status = access.request("openrouter", "video.status", {"id": "job-1"})
    result = access.request("openrouter", "video.content", {"id": "job-1", "index": 0})
    assert [r.url.host for r in calls] == ["openrouter.ai"] * 3
    assert status["usage"]["cost"] == 0.01
    assert result["artifacts"][0]["path"].endswith(".mp4")


def test_google_async_operation_and_safe_file_download(access, intercept):
    def handler(request):
        if request.url.path.endswith(":download"):
            return httpx.Response(200, content=b"mp4", headers={"content-type": "video/mp4"})
        return reply({"done": True})
    calls = intercept(handler)
    access.request("google", "operation.status", {"name": "models/new-model/operations/op1"})
    access.request("google", "file.content", {"name": "https://generativelanguage.googleapis.com/v1beta/files/video1:download?alt=media"})
    assert calls[0].url.path == "/v1beta/models/new-model/operations/op1"
    assert calls[1].url.path == "/v1beta/files/video1:download"
    assert calls[1].url.params["alt"] == "media"


@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_background_response_can_be_retrieved(access, intercept, provider):
    calls = intercept(lambda request: reply({"id": "resp-123", "status": "completed", "output": []}))
    result = access.request(provider, "responses.status", {"id": "resp-123"})
    assert result["response"]["status"] == "completed"
    assert calls[0].method == "GET"
    assert calls[0].url.path.endswith("/responses/resp-123")
    with pytest.raises(ValueError):
        access.request(provider, "responses.status", {"id": "resp-123", "url": "https://evil"})


@pytest.mark.parametrize("location", ["https://storage.googleapis.com/generated/video.mp4?X-Goog-Signature=abc",
                                     "https://media.googleusercontent.com/video.mp4",
                                     "/download/v1beta/files/video1:download?alt=media"])
def test_google_file_download_follows_only_trusted_redirects(access, intercept, location):
    def handler(request):
        if request.url.path == "/v1beta/files/video1:download":
            return httpx.Response(302, headers={"location": location})
        if request.url.host != "generativelanguage.googleapis.com":
            assert "x-goog-api-key" not in request.headers
            assert "authorization" not in request.headers
        else:
            assert request.headers["x-goog-api-key"] == SECRET
        return httpx.Response(200, content=b"video bytes", headers={"content-type": "video/mp4"})
    calls = intercept(handler)
    result = access.request("google", "file.content", {"name": "files/video1"})
    assert len(calls) == 2
    assert result["artifacts"][0]["bytes"] == 11


@pytest.mark.parametrize("location", ["https://storage.googleapis.com.evil.test/video", "http://storage.googleapis.com/video",
                                     "https://127.0.0.1/video", "https://storage.googleapis.com:444/video",
                                     "https://storage.googleapis.com/video?key=something", "https://generativelanguage.googleapis.com/v1beta/models"])
def test_google_redirect_refuses_other_origins_ports_and_credential_queries(access, intercept, location):
    calls = intercept(lambda request: httpx.Response(302, headers={"location": location}))
    with pytest.raises(ModelAccessError):
        access.request("google", "file.content", {"name": "files/video1"})
    assert len(calls) == 1


def test_google_redirect_chain_has_three_hop_limit(access, intercept):
    calls = intercept(lambda request: httpx.Response(302, headers={"location": "https://media.googleusercontent.com/loop"}))
    with pytest.raises(ModelAccessError, match="not forwarded"):
        access.request("google", "file.content", {"name": "files/video1"})
    assert len(calls) == 4


def test_shared_model_outputs_are_readable_by_sandbox_uid(access, intercept):
    import os
    import stat
    (access.ws.work / "model-output").mkdir(mode=0o700)
    intercept(lambda request: reply({"text": "hello"}))
    previous_umask = os.umask(0o077)
    try:
        result = access.request("openrouter", "embeddings", {"model": "new", "input": "hello"})
    finally:
        os.umask(previous_umask)
    path = access.ws.work / result["response_path"]
    assert stat.S_IMODE(path.stat().st_mode) & 0o004
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o005 == 0o005
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) & 0o005 == 0o005


def test_image_response_media_type_controls_output_extension(access, intercept):
    intercept(lambda request: reply({"data": [{"media_type": "image/webp", "b64_json": base64.b64encode(b"webp").decode()}]}))
    result = access.request("openrouter", "images.generate", {"model": "new", "prompt": "x"})
    assert result["artifacts"][0]["path"].endswith(".webp")


@pytest.mark.parametrize("bad", ["https://evil.example/file", "https://generativelanguage.googleapis.com.evil.test/v1beta/files/x", "https://generativelanguage.googleapis.com/v1beta/files/x?key=steal", "files/../secret", "files/x%2fsecret", "https://user@generativelanguage.googleapis.com/v1beta/files/x"])
def test_google_download_cannot_send_key_to_other_routes(access, intercept, bad):
    calls = intercept(lambda request: pytest.fail("must reject before HTTP"))
    with pytest.raises(ValueError):
        access.request("google", "file.content", {"name": bad})
    assert not calls


@pytest.mark.parametrize("bad", ["../secret", "https://evil.example", "id?key=evil", "id/../../billing", ".."])
def test_resource_id_rejects_path_injection(access, bad):
    with pytest.raises(ValueError):
        access.request("openrouter", "video.content", {"id": bad})


@pytest.mark.parametrize("path", ["../host-key", "/etc/passwd", ".env", "nested/.env.local", "a/../x", "a\\b"])
def test_file_marker_refuses_escape_and_envfiles(access, path):
    with pytest.raises(ValueError):
        access.request("openrouter", "embeddings", {"model": "new", "input": {"$file": path, "encoding": "text"}})


def test_file_marker_refuses_symlink_and_fifo_without_blocking(access, tmp_path):
    import os
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    (access.ws.work / "link.txt").symlink_to(secret)
    os.mkfifo(access.ws.work / "pipe")
    for path in ("link.txt", "pipe"):
        with pytest.raises(ValueError):
            access.request("openrouter", "embeddings", {"model": "new", "input": {"$file": path, "encoding": "text"}})


def test_zero_byte_file_marker_count_is_bounded(access, intercept):
    (access.ws.work / "empty.txt").touch()
    calls = intercept(lambda request: pytest.fail("must reject before HTTP"))
    with pytest.raises(ValueError, match="128 file markers"):
        access.request("openrouter", "embeddings", {"model": "new", "input": [
            {"$file": "empty.txt", "encoding": "text"} for _ in range(129)]})
    assert not calls


def test_file_marker_path_length_is_bounded(access):
    with pytest.raises(ValueError, match="relative"):
        access.request("openrouter", "embeddings", {"model": "new", "input": {"$file": "x" * 4097, "encoding": "text"}})


def test_output_parent_symlink_is_rejected(access, tmp_path, intercept):
    (access.ws.work / "model-output").symlink_to(tmp_path / "outside", target_is_directory=True)
    intercept(lambda request: pytest.fail("must reject before HTTP"))
    with pytest.raises(ModelAccessError, match="symbolic links"):
        access.request("openrouter", "embeddings", {"model": "new", "input": "hi"})


@pytest.mark.parametrize("extra", [{"tools": [{"type": "web_search"}]}, {"plugins": [{"id": "web"}]},
                                  {"mcp_servers": [{"url": "https://evil"}]}, {"stream": True},
                                  {"headers": {"x": "bad"}}, {"base_url": "https://evil"}])
def test_no_hosted_tool_or_arbitrary_proxy_bypass(access, intercept, extra):
    calls = intercept(lambda request: pytest.fail("must reject before HTTP"))
    with pytest.raises(ValueError):
        access.request("openrouter", "chat.completions", {"model": "new", "messages": [], **extra})
    assert not calls


def test_keys_are_redacted_from_json_and_saved_response(access, intercept):
    intercept(lambda request: reply({"text": f"echo {SECRET}", SECRET: [SECRET], "usage": {"prompt_tokens": 1}}))
    result = access.request("openrouter", "embeddings", {"model": "new", "input": "x"})
    assert SECRET not in json.dumps(result)
    assert SECRET not in (access.ws.work / result["response_path"]).read_text()
    assert SECRET not in (access.ws.ledger / "tools.jsonl").read_text()


def test_upstream_error_body_and_headers_never_exposed(access, intercept):
    intercept(lambda request: httpx.Response(403, json={"error": f"key={SECRET}"}, headers={"x-key": SECRET}))
    with pytest.raises(ModelAccessError, match="HTTP 403") as error:
        access.request("openrouter", "embeddings", {"model": "new", "input": "x"})
    assert SECRET not in str(error.value)
    assert SECRET not in (access.ws.ledger / "tools.jsonl").read_text()


def test_structured_provider_error_has_actionable_bounded_redacted_detail(access, intercept):
    intercept(lambda request: reply({"error": {"message": f"Unsupported size 1024x2048. Key {SECRET}",
                                               "code": "unsupported_size", "param": "size"}}, 400))
    with pytest.raises(ModelAccessError) as error:
        access.request("openrouter", "images.generate", {"model": "new/image", "prompt": "x"})
    assert "Unsupported size 1024x2048" in str(error.value)
    assert "unsupported_size" in str(error.value)
    assert "untrusted" in str(error.value)
    assert SECRET not in str(error.value)


def test_huge_error_response_is_not_forwarded(access, intercept):
    intercept(lambda request: reply({"error": {"message": "x" * 10000 + SECRET}}, 400))
    with pytest.raises(ModelAccessError) as error:
        access.request("openrouter", "images.generate", {"model": "new/image", "prompt": "x"})
    assert len(str(error.value)) < 200
    assert SECRET not in str(error.value)


def test_redirect_does_not_forward_key(access, intercept):
    calls = intercept(lambda request: httpx.Response(302, headers={"location": "https://evil.test/key"}))
    with pytest.raises(ModelAccessError, match="not forwarded"):
        access.request("google", "file.content", {"name": "files/x"})
    assert len(calls) == 1


def test_http_failure_error_is_safe(access, intercept):
    def handler(request):
        raise httpx.ReadTimeout(SECRET)
    intercept(handler)
    with pytest.raises(ModelAccessError, match="timed out") as error:
        access.request("openrouter", "embeddings", {"model": "new", "input": "x"})
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("content", ["not-base64", ""])
def test_bad_or_empty_inline_media_fails(access, intercept, content):
    intercept(lambda request: reply({"data": [{"b64_json": content}]}))
    with pytest.raises(ModelAccessError):
        access.request("openrouter", "images.generate", {"model": "new/image", "prompt": "x"})


def test_artifact_size_cap_removes_partial_binary(access, intercept, monkeypatch):
    monkeypatch.setattr(module, "MAX_ARTIFACT_BYTES", 3)
    intercept(lambda request: httpx.Response(200, content=b"too-big", headers={"content-type": "video/mp4"}))
    with pytest.raises(ModelAccessError, match="256 MiB"):
        access.request("openrouter", "video.content", {"id": "job"})
    assert not list(access.ws.work.glob("model-output/*/output*"))


@pytest.mark.parametrize("cancel", ["deadline", "event"])
def test_deadline_and_cancellation_prevent_network(access, intercept, cancel):
    calls = intercept(lambda request: pytest.fail("must not send"))
    if cancel == "deadline":
        access.deadline = time.monotonic() - 1
    else:
        access.cancel_event = threading.Event()
        access.cancel_event.set()
    with pytest.raises(ModelAccessError):
        access.request("openrouter", "embeddings", {"model": "new", "input": "x"})
    assert not calls


def test_payload_size_and_finite_json_before_network(access, intercept, monkeypatch):
    calls = intercept(lambda request: pytest.fail("must not send"))
    monkeypatch.setattr(module, "MAX_INPUT_JSON", 30)
    with pytest.raises(ValueError):
        access.request("openrouter", "embeddings", {"model": "new", "input": "x" * 100})
    with pytest.raises(ValueError):
        access.request("openrouter", "embeddings", {"model": "new", "input": float("nan")})
    assert not calls


def test_model_route_name_is_not_an_arbitrary_google_path(access):
    with pytest.raises(ValueError):
        access.request("google", "generate_content", {"model": "../billing", "contents": []})


def test_batch_embedding_keeps_requests_native(access, intercept):
    calls = intercept(lambda request: reply({"embeddings": [{"values": [0.1]}]}))
    payload = {"model": "new-embed", "requests": [{"model": "models/new-embed", "content": {"parts": [{"text": "x"}]}}]}
    access.request("google", "batch_embed_contents", payload)
    assert calls[0].url.path.endswith(":batchEmbedContents")
    assert json.loads(calls[0].content)["requests"] == payload["requests"]


@pytest.mark.parametrize(("provider", "operation", "payload", "native"), [
    ("openrouter", "video.status", {"id": "job-usage"}, {"id": "job-usage", "status": "completed", "usage": {"cost": 0.5}}),
    ("openai", "responses.status", {"id": "resp-usage"}, {"id": "resp-usage", "status": "completed", "usage": {"input_tokens": 5, "output_tokens": 8}}),
    ("google", "operation.status", {"name": "operations/op-usage"}, {"name": "operations/op-usage", "done": True,
        "response": {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 8}}}),
])
def test_repeated_and_concurrent_poll_usage_counted_once(access, intercept, provider, operation, payload, native):
    from concurrent.futures import ThreadPoolExecutor
    intercept(lambda request: reply(native))
    with ThreadPoolExecutor(max_workers=6) as workers:
        results = list(workers.map(lambda _i: access.request(provider, operation, payload), range(6)))
    records = [json.loads(line) for line in (access.ws.ledger / "tools.jsonl").read_text().splitlines()]
    assert len(records) == 6
    assert sum(bool(row["usage"]) for row in records) == 1
    assert all(result["usage"] for result in results)
    assert sum(result["accounting"]["state"] == "recorded" for result in results) == 1
    assert sum(result["accounting"]["state"] == "already_recorded" for result in results) == 5
    assert not (access.ws.work / ".model-access").exists()
    assert len(list((access.ws.root / ".model-access" / "usage").glob("*.json"))) == 1


def test_pending_response_usage_waits_for_completed_final_usage(access, intercept):
    def handler(request):
        if request.method == "POST":
            return reply({"id": "resp-job", "status": "queued", "usage": {"input_tokens": 3}})
        return reply({"id": "resp-job", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 9}})
    intercept(handler)
    initial = access.request("openai", "responses", {"model": "new", "input": "hello", "background": True})
    first = access.request("openai", "responses.status", {"id": "resp-job"})
    # A separate process/worker receives the same private accounting claim.
    second = ModelAccess(access.ws, "other-agent").request("openai", "responses.status", {"id": "resp-job"})
    assert initial["usage"] == {"input_tokens": 3}
    assert initial["accounting"]["state"] == "pending"
    assert first["accounting"]["state"] == "recorded"
    assert second["accounting"]["state"] == "already_recorded"
    records = [json.loads(line) for line in (access.ws.ledger / "tools.jsonl").read_text().splitlines()]
    assert [row["usage"] for row in records] == [{}, {"input_tokens": 3, "output_tokens": 9}, {}]


def test_immediately_completed_video_create_and_status_share_usage_claim(access, intercept):
    intercept(lambda request: reply({"id": "job-immediate", "status": "completed", "usage": {"cost": 0.4}}))
    initial = access.request("openrouter", "video.create", {"model": "new/video", "prompt": "hello"})
    final = access.request("openrouter", "video.status", {"id": "job-immediate"})
    assert initial["accounting"]["state"] == "recorded"
    assert final["usage"] == {"cost": 0.4}
    assert final["accounting"]["state"] == "already_recorded"
    records = [json.loads(line) for line in (access.ws.ledger / "tools.jsonl").read_text().splitlines()]
    assert sum(row["usage"].get("cost", 0) for row in records) == 0.4
