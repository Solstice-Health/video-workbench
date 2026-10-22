# Model access from tools and scripts

The chat agent and its workers can use configured OpenAI, Google, Anthropic and
OpenRouter APIs independently of the model answering the conversation. This
supports model-assisted scripts, embeddings and supported image, audio and video
operations through the same registered toolkit. Changing the chat model is not
required.

## Configure the host

Put the required keys in the host `.env`, or use **Models and APIs → Provider
APIs**. The accepted names are `OPENAI_API_KEY`, `GOOGLE_API_KEY`,
`ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY`. `GEMINI_API_KEY` is also accepted for
Google when `GOOGLE_API_KEY` is absent. Within each source, the Google canonical
name takes precedence; process environment settings take precedence over `.env`.
UI overrides take precedence over both, including an explicit disconnect.

`VWB_ENV_FILE` selects an explicit environment file; otherwise Workbench finds
the nearest `.env` above the checkout or launch directory. The host reads these
keys directly. Keys are never copied into chat, scripts, images or agent
containers. A script uses a bounded request bridge instead of reading `.env` or
receiving a provider API key.

## Discover before requesting

- `model_providers` reports configuration status, allowed operations and their
  request guidance. It returns no credential values.
- `model_catalog(provider, query="", modality="", limit=30, offset=0)` reads
  the provider's current catalog. Page through `next_offset` rather than assuming
  the first page contains every model. Use current IDs and advertised capabilities
  instead of guessing names from a model family. OpenRouter's modality filter
  selects output capabilities: `video` means video generation. To choose a model
  that can watch video or read images, inspect its advertised input modalities.
- `model_request(provider, operation, payload, purpose="")` makes one supported
  native API operation. `payload` uses that provider's request shape. Use the
  discovered operation guidance and official API documentation for fields that
  depend on a specific model.

`model_providers` is the live source of supported operation names. Operations
include OpenAI Responses/chat, image generation and editing, embeddings, speech,
transcription and video; OpenRouter's supported chat/multimodal and audio routes;
Google content generation, embeddings and prediction/video operations; and
Anthropic Messages. These are native provider requests rather than one fabricated
schema shared by every model. Optional model-specific fields remain available
inside the native payload.

A configured key establishes that the host has a credential. It does not prove
that every listed model is enabled for the account, that billing is active, or
that every input/output modality is supported. Some catalogs do not advertise
modalities; the catalog reports when a requested modality filter could not be
applied. Absence of that metadata is not a capability guarantee. Streaming
Realtime/WebSocket sessions and arbitrary API endpoints are not provided by this
request bridge.

Media policies also apply to general chat, Responses and Google content APIs,
whose chosen model may produce media by default. If an image, video or audio
capability is set to `block`, `ask` or `flag`, these general endpoints pass that
gate conservatively before dispatch, even when a caller asks for text only.
Dedicated text/embedding operations retain their ordinary model policy.

The generic request tool complements the existing Sunburst and Omni tools. Prefer
those tools when their registered extraction, inpainting, outpainting, masking,
keying and composition workflows fit the task.

## Use a script

Write a Python script under `work/`, then call `sandbox_run` with `models=true`.
The injected `vwb_models` helper needs only the Python standard library and works
in request-local virtual environments. Python's isolated `-I` mode ignores its
injected `PYTHONPATH`; use normal Python execution when importing the helper.

```python
import json
from vwb_models import ModelError, catalog, providers, request

print(json.dumps(providers(), indent=2))
print(json.dumps(catalog("openrouter", modality="text"), indent=2))

# Choose an actual model ID from discovery for the task before running a call.
model_id = "<discovered-model-id>"
try:
    result = request(
        "openrouter",
        "chat.completions",
        {"model": model_id,
         "messages": [{"role": "user", "content": "Summarize this editing brief."}],
         "max_tokens": 512},
        purpose="Summarize the current editing brief",
    )
    print(json.dumps(result, indent=2))
except ModelError as error:
    raise SystemExit(str(error))
```

Tool invocation:

```json
{"argv":["python","summarize.py"],"models":true,"network":"none"}
```

The helper signatures match the tools:

```python
providers()
catalog(provider, query="", modality="", limit=30, offset=0)
request(provider, operation, payload, purpose="")
```

The bridge works with `network="none"` because the host makes the provider
request. `network="public"` is only needed for the script's separate package
downloads or public HTTP requests. Background sandbox jobs can use the bridge
too. Every bridged call still passes through Registry with the originating
agent's role, policy gates, limits and logging; it does not grant broader tools.

## Examples by task

The model and voice placeholders below must be replaced with IDs and options
supported by the chosen provider. These are examples of native payload shapes,
not a promise that one model supports every operation.

Embeddings, including reading a large result saved outside the tool summary:

```python
import json
from vwb_models import request

result = request("openai", "embeddings", {
    "model": "<embedding-model-id>",
    "input": ["Close-up of an animated DNA ribbon", "A wide landscape shot"],
}, purpose="Embed shot descriptions for semantic retrieval")
with open(result["response_path"]) as handle:
    full = json.load(handle)
vectors = [item["embedding"] for item in full["response"]["data"]]
```

Google image generation with a workspace image as reference:

```python
image = request("google", "generate_content", {
    "model": "<image-capable-model-id>",
    "contents": [{"role": "user", "parts": [
        {"text": "Recreate this illustration on a transparent background."},
        {"inlineData": {"mimeType": "image/png", "data": {
            "$file": "references/source.png", "encoding": "base64"}}},
    ]}],
    "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
}, purpose="Create an illustration variant from the supplied reference")
print(image["artifacts"])
```

Google text-to-speech with a voice supported by the selected model:

```python
speech = request("google", "generate_content", {
    "model": "<speech-capable-model-id>",
    "contents": [{"parts": [{"text": "Read in a calm voice: Welcome to the workbench."}]}],
    "generationConfig": {
        "responseModalities": ["AUDIO"],
        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
            "voiceName": "<documented-voice-name>"}}},
    },
}, purpose="Create narration for the current draft")
print(speech["artifacts"])
```

For OpenAI speech, use `audio.speech` with the selected `model`, `input`, `voice`
and optional `response_format`. Its binary response is saved as an audio artifact.
Google raw PCM with advertised sample-rate metadata is wrapped as WAV; other raw
PCM remains explicit and requires the model's documented format when converting.

An OpenAI video job is submitted once, then polled in separate calls:

```python
job = request("openai", "video.create", {
    "model": "<video-model-id>", "prompt": "A slow camera move over a sand dune.",
}, purpose="Create a background clip")
job_id = job["response"]["id"]
status = request("openai", "video.status", {"id": job_id})
# Poll later if still queued/in_progress; do not submit the creation again.
if status["response"]["status"] == "completed":
    clip = request("openai", "video.content", {"id": job_id})
    print(clip["artifacts"])
```

For Google video, submit `predict_long_running`, preserve the returned `name`,
and query `operation.status` with that name. A completed operation may contain a
Gemini file URI; retrieve it through `file.content` with `name` set to the returned
URI or `files/<id>`. These content operations accept only their fixed provider's
file endpoint, not arbitrary URLs.

## Files and generated outputs

Refer to an input file with a work-relative marker inside the native payload:
`{"$file":"references/source.png","encoding":"data_url"}`. Use
`encoding="base64"` or `encoding="text"` when that is the native field's expected
format. An optional `mime_type` names the input type. Multipart inputs use a
`$file` marker without an encoding, for example an OpenAI image-edit `image` or
transcription `file` field. This lets
scripts work with durable paths rather than putting binary blobs in chat.

Results identify the provider, operation and model, and include the native
`response`, reported `usage`, `artifacts` and `response_path`. Generated binaries
and the complete response envelope are retained under `work/model-output/<call-id>/`.
The saved JSON contains the native provider output under `response`. Large tool
results replace the inline `response` with `null` and set `response_truncated`;
scripts can read the saved envelope as shown above. Returned paths are relative
to the `work/` root, which is the sandbox's default working directory.
Artifact entries include path, MIME type, byte count and SHA-256, with an asset
reference when imported into the asset registry. The agent can inspect or reuse
those paths in later turns; compaction does not remove the files.
The saved envelope precedes asset registration; assigned `asset_ref` values are
in the returned tool result and ledger, alongside the recorded input manifest.

Some video APIs start a remote operation rather than returning a finished video.
Keep its returned operation ID and poll the matching status operation explicitly,
then retrieve its output through the supported content operation. A submitted
operation is not a completed asset. Provider errors, rate limits and incomplete
operations remain visible; the bridge does not silently substitute another model.

OpenAI and OpenRouter Responses calls using `background=true` can be retrieved
with `responses.status` and `{"id":"<returned-response-id>"}`. Preserve that ID
instead of submitting the same creation again. File-marker inputs also retain
an `inputs` manifest with the exact uploaded byte counts, MIME types and SHA-256
hashes in the response envelope and request log, so editing a file at the same
path changes the recorded input identity.

Inline media and binary content responses are saved locally. If a legacy image
model returns only a remote URL, that URL remains explicit in its native response;
use the existing public asset-fetch workflow to import it before composition.
The URL alone is not a registered or verified asset; do not automatically publish
signed download links returned by a provider.
The model gateway does not fetch arbitrary returned URLs. Google's generated-file
download is the bounded exception: it follows only permitted Google file/CDN
redirects and strips the API credential before contacting a CDN.

The main prompt and every worker role describe this capability. Native resumed
threads receive the existing harness-change notice when tools or instructions
change. Already queued work retains its captured prompt; current role and policy
checks still apply when tools run.
