# Public web tools

The generation agent can call `web_search`, `web_fetch`, and `web_research` through
the same registry, role checks, policy dial and call log as its other tools. The
main agent chooses whether to call them. A stateless OpenRouter research model
receives only the explicit query, focus and URLs, with search/fetch tools and no
workspace or editing tools. The main generation model remains unchanged.

## Setup and operation

Set `OPENROUTER_API_KEY` in the nearest `.env` above the repository, or select a file
with `VWB_ENV_FILE`. The normal installation uses `../.env`. Environment variables
take precedence. Credentials stay on the host and are not copied into the agent
container, request body, tool result or browser. `vwb doctor` reports presence only.

`config/slots.yaml` contains the `web.research` slot. Its initial model is
`openai/gpt-5.4-mini`, with Exa pinned for both search and fetch. The model and
supported extraction engines can be changed there without changing the main agent.
OpenRouter bills the inference and any tool charges; an Exa key is not needed for
this path. The existing `EXA_API_KEY` is reserved for a future direct adapter.

| Tool | Input | Budget and output |
|---|---|---|
| `web_search` | `query`, optional `domains`, `max_results` | At most 2 searches, 1–10 results each, source-linked model summary |
| `web_fetch` | Public `url`, optional `focus` | One fetch, restricted to the supplied hostname; focused model summary of a page or PDF |
| `web_research` | `task`, optional `domains`, `max_steps` | Default 6, maximum 8 total server-tool calls; at most 3 searches and 5 fetches |

Fetch content is capped at approximately 12,000 tokens and model output at 4,000
tokens. Network inactivity times out after 180 seconds (connection establishment
after 30 seconds); this is not an absolute wall-clock deadline. Failed billed loops
are not automatically retried. OpenRouter's own server tools perform the inner search/read loop.

The job lead, planner and composer have access. The scene miner and critic retain
their previous allowlists. `web.search`, `web.fetch`, and `web.research` are separate
policy capabilities; combined research additionally requires both individual
capabilities to be `allow`. Use individual tools when their policy requires asking
or flagging. New turns rebuild the registry; a process already running with the old
registry must finish or be restarted to see the tools.

## Evidence and boundaries

Responses include the model's answer, provider citation annotations when supplied,
reported tool activity, model/response identifiers, completion time and provider
usage. Token usage is normalized into the existing cumulative accounting without
changing the main thread's measured context. Provider cost fields are retained in
the call log; they are not a locally estimated all-in bill.

These are model-mediated retrieval tools, not raw search/HTML APIs. A model-written
URL is not promoted to a provider citation. An executed-tool count does not prove
successful page extraction. Missing retrieval evidence is labeled unverified;
source freshness is not inferred from the request completion time.

Only public HTTP(S) URLs without embedded credentials or custom ports are accepted
by `web_fetch`. Literal private/loopback addresses and local hostnames are rejected.
URL retrieval is remote; there is no host-side HTTP fallback. OpenRouter and its
engine handle remote DNS, redirects and page extraction. These validations are not
a network isolation boundary for the existing Codex container's shell.

Web content is untrusted evidence, never an instruction source, approved copy or a
licence grant. Do not put credentials or private briefs in queries. Web research
does not download production assets, resolve provenance flags, grant rights, or
change the source-faithfulness requirements. Interactive clicking, login sessions,
screenshots and asset quarantine are not implemented here.

## Provider choices researched on 2026-10-12

The following fit judgments are recommendations based on each provider's documented
capabilities, not a comparative benchmark.

| Option | Advantage for this project |
|---|---|
| [OpenRouter server tools](https://openrouter.ai/docs/guides/features/server-tools) | One credential and a hosted search/read loop. Best immediate fit; server tools are beta. |
| [Exa Search](https://exa.ai/docs/reference/search) and [Contents](https://exa.ai/docs/reference/get-contents) | Direct structured results, source text and retrieval controls. A useful next adapter when the main agent should receive raw evidence without an intermediate synthesis. |
| [Tavily](https://docs.tavily.com/documentation/api-reference/introduction) | Search, extraction, crawl and map in one retrieval toolkit, plus hosted research. |
| [Firecrawl](https://docs.firecrawl.dev/features/scrape) | Markdown, structured extraction, screenshots and page actions for difficult or visual source pages. |
| [Browserbase](https://docs.browserbase.com/welcome/introduction) and [Stagehand](https://docs.stagehand.dev/v3/first-steps/introduction) | Managed browser sessions and agent-driven interaction when actual navigation is needed; Browserbase also offers search/fetch. |
| [Playwright Python](https://playwright.dev/python/docs/library) | Direct control of browser navigation and screenshots in the existing Python stack; we must operate the browser and research loop ourselves. |

OpenRouter's [search guide](https://openrouter.ai/docs/guides/features/server-tools/web-search)
and [fetch guide](https://openrouter.ai/docs/guides/features/server-tools/web-fetch)
define the request parameters and current engines. Use `openrouter:web_search` and
`openrouter:web_fetch` server tools; the older web plugin and `:online` approach are
deprecated. Search discovers URLs, fetch reads them, and neither is an interactive
browser.

## Verification

Live smoke tests on 2026-10-12 called all three tools through the actual registry:
search returned official Python documentation and provider citations; fetch read
`example.com`; research reported two executed server-tool calls and a cited answer
after being asked to search and read the Python documentation. The provider reported
one search in that research call, but did not expose a full per-step fetch trace.
All three requests appeared in the existing token accounting. These calls exercised
the host toolkit, not a full video-generation turn in Docker.

Mocked tests cover API errors, malformed and unfinished answers, credential handling,
usage accounting, retrieval evidence, input limits, role permissions and policy gates.
The full suite passed: 121 tests. The changed web modules and their tests pass Ruff.
