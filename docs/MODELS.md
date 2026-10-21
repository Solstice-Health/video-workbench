# Main-agent models and provider APIs

Open the model pill below the chat input to choose the main agent. Context and
Effort open side menus for that model. The small expand control opens **Models and
APIs**, where you choose which models appear in the compact picker and manage
provider credentials. A selected model remains visible until you choose another.

## Available models

The curated catalog lives in `config/models.yaml`: Astra (`gpt-6-astra`), 6.1 Sol
(`gpt-6.1-sol`), Fable 5.1 (`claude-fable-5-1`), Opus 5.5 (`claude-opus-5-5`),
and Muse Wonder (`meta/muse-wonder` through OpenRouter). 6 Sol (`gpt-6-sol`) can
also be enabled in the manager.

Choose **OpenRouter model…** to enter an exact ID, including the provider:
`openrouter/meta/muse-wonder`. The backend checks OpenRouter's current catalog
and requires tool calling and a reported context limit. Custom models expose
reasoning effort only when their catalog metadata explicitly reports it. Unknown
or unsupported IDs fail before an agent turn starts.

Anthropic models always use `https://api.anthropic.com` and the host's
`ANTHROPIC_API_KEY`, loaded from the nearest project `.env` unless a provider
override is explicitly saved. They never use the OpenRouter key. Curated
Anthropic aliases entered through the custom model field (for example
`anthropic/claude-opus-5.5`) resolve to the verified native model ID without
contacting OpenRouter. Unsupported aliases fail before sending a generation.
Saved OpenRouter Claude configurations and legacy Claude SDK fallback entries
are routed to the direct Anthropic API on their next turn. Provider histories
stay separate; the new native history receives the saved public conversation.

OpenAI models run in the existing Codex Docker host. Anthropic and OpenRouter
models use persistent host-side API tool loops, with tools routed through the
same registry and role permissions. Each provider/model keeps its own history;
switching models supplies saved conversation and request state. The picker changes
the main agent; worker and generation-tool slots keep their configured models.

## Context, effort and speed

Context selects a **working budget**, not an extension of the provider's actual
model limit. Choose 300K or 1M where the model supports it. A custom model below
300K offers its reported native limit instead. Earlier messages remain saved.

Effort offers Low, Medium, High, Extra high and Max where supported. The slider
commits when released or adjusted with the keyboard. Models without an explicit
effort capability use their provider default.

Fast mode is enabled for Astra, 6.1 Sol, 6 Sol and Opus 5.5. Ultra fast is enabled
for Astra and 6.1 Sol. Fable 5.1 and Muse Wonder have no enabled speed controls in
the bundled catalog. Opus fast requires Anthropic Fast mode access and has higher
rates. Custom OpenRouter speed controls depend on its reported endpoint tags.
Fast and Ultra fast are mutually exclusive; access remains subject to the provider
account, and a rejected request is surfaced as an error.

Context capacity and output capacity are separate. A 1M context budget does not
allow a single call to generate 1M tokens. Direct Anthropic models allow up to
128K output tokens per call, including thinking. The previous fixed 16K output
setting could exhaust that allowance in the main agent at Max effort before it
could finish its response; this was independent of subagents and context fullness.

Codex receives the selected context budget and an automatic-compaction threshold
at 85% of that budget. Direct Anthropic calls enable provider-native automatic
compaction at the configured threshold. The latest context measurement describes
the final iteration after compaction, while conversation billing includes all
reported iterations. The context API also reports the output allowance,
automatic-compaction threshold/status, compaction count and last stop reason when
the provider host has reported them. Manual **Compact conversation** is currently
available only for Codex threads.

Direct Anthropic/OpenRouter calls also perform a conservative history-size
preflight, reserving space for output. Text is conservatively counted, and inline
images are estimated from their dimensions rather than base64 transport size.
Anthropic uses its visual patch formula; custom OpenRouter image accounting is
approximate because encoders differ. These estimates are not measured usage, and
providers may reject a request that passes preflight. If the estimate exceeds the
budget for a supported Anthropic model with native compaction, the harness first
uses Anthropic's token-counting endpoint. It proceeds only when the measured
context fits, or when native compaction can reduce it at the configured trigger.
Other oversized requests are rejected before generation. Choose a larger supported
budget or start a new conversation when the request cannot fit.

## When selections apply

Changes apply to future submissions. An active turn and messages already queued
keep the settings captured when they were submitted. Choosing a model before
creating a conversation saves the default; a conversation's choice is stored in
its own `memory/model_selection.json`. Explicit selections use that provider
without silently falling back to a different model.

## Provider keys

The Provider APIs tab manages OpenAI, Anthropic, Google and OpenRouter. **Configured**
means a key is present; it does not verify account access. Password fields start
empty, and saved keys are never returned to the browser or put in browser storage.

Baseline keys come from environment variables or the nearest `.env` above the
repository; `VWB_ENV_FILE` can select another file. Workbench overrides are stored
only on the host in `runs/.keys/provider_keys.json`, with file permissions `0600`
and directory permissions `0700`. Overrides take precedence over baseline keys.
This is a private local JSON file, not an encrypted credential vault.

**Disconnect** suppresses that provider's baseline key without editing `.env`.
**Use .env key** removes the Workbench override and restores the baseline key when
one exists. Keys remain in the control plane, outside agent containers and work
folders. Direct Anthropic selections and the Claude fallback require an API key;
an expired or rejected key is surfaced as an authentication error.

Implementation: `src/vwb/models.py`, `config.py`, `agents/provider_loop.py`, and
`web/static/model-control.js`. Model/provider controls were checked with mocked
API and DOM tests; those checks do not validate paid access to every model or tier.
