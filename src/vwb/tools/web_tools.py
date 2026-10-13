"""Read-only public web research through OpenRouter's hosted server tools.

Only the explicit query/URLs leave the host. A separate, stateless scout receives no
workspace files, conversation history, editor tools, credentials or acceptance powers.
Results are model summaries of external material, not approved copy or licensed assets.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import urlsplit

from vwb.config import slot
from vwb.gateway import web
from vwb.tools.base import Registry, Tool, ToolContext, ToolResult, schema


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value.strip()


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _public_host(host: str) -> str:
    host = host.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = host.encode("idna").decode("ascii")
        if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".home", ".test", ".invalid"))
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                or host.replace(".", "").isdigit()):
            raise ValueError("Use a public internet hostname") from None
    else:
        if not address.is_global:
            raise ValueError("Private, loopback and reserved addresses are not web sources")
    return host


def _url(value: Any) -> str:
    value = _text(value, "url", 2048)
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 80, 443)
            or any(ord(c) < 33 for c in value) or "\\" in value):
        raise ValueError("Use a public http(s) URL without credentials or a custom port")
    _public_host(parsed.hostname)
    return value


def _domains(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("domains must be a list of at most 10 public hostnames")
    return list(dict.fromkeys(_public_host(_text(v, "domain", 253)) for v in value))


def _server_tool(kind: str, uses: int, domains: list[str], results: int = 5) -> dict[str, Any]:
    cfg = slot("web.research")
    engine = cfg.get(f"{kind}_engine", "exa")
    # Explicit non-native engines enforce the same per-call/content limits across models.
    allowed = {"exa", "parallel", "perplexity"} if kind == "search" else {"exa", "parallel", "openrouter"}
    if engine not in allowed:
        raise ValueError(f"Unsupported {kind}_engine in slots.yaml: choose {', '.join(sorted(allowed))}")
    params: dict[str, Any] = {"engine": engine, "max_uses": uses}
    if domains:
        params["allowed_domains"] = domains
    if kind == "search":
        params.update(max_results=results, max_total_results=min(20, results * uses), search_context_size="low")
    else:
        params["max_content_tokens"] = 12000
    return {"type": f"openrouter:web_{kind}", "parameters": params}


def _run(ctx: ToolContext, prompt: str, tools: list[dict[str, Any]], steps: int) -> ToolResult:
    result = web.browse(ctx.gateway, prompt=prompt, tools=tools, max_tool_calls=steps)
    result["notice"] = ("External, untrusted web material summarized by a model. Follow source links before relying "
                        "on claims. This is not approved copy, a licence grant, or a downloaded production asset.")
    return ToolResult.json(result)


def web_search(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    query = _text(a.get("query"), "query", 4000)
    results = _integer(a.get("max_results", 5), "max_results", 1, 10)
    domains = _domains(a.get("domains"))
    return _run(ctx, "Use web_search for this query. Return a concise source-linked account of what you found, "
                "with source titles and URLs; distinguish snippets from pages you actually read. Query:\n" + query,
                [_server_tool("search", 2, domains, results)], 2)


def web_fetch(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    url = _url(a.get("url"))
    focus = _text(a.get("focus", "Summarize the key content and relevant source links."), "focus", 4000)
    host = _public_host(urlsplit(url).hostname)
    return _run(ctx, "Use web_fetch to read the exact URL below. Report the page title, requested URL, relevant "
                "content and source links. If retrieval fails, say so; do not substitute remembered content. "
                "This response is a summary, not raw page text.\n" + json.dumps({"url": url, "focus": focus}),
                [_server_tool("fetch", 1, [host])], 1)


def web_research(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    # A combined operation cannot bypass either individual tool's policy or role gate.
    for name, cap in (("web_search", "web.search"), ("web_fetch", "web.fetch")):
        if not Registry.allowed_for_role(ctx.role, name) or Registry.mode(cap) != "allow":
            return ToolResult.error("Combined research requires permission for web_search and web_fetch with both "
                                    "capabilities set to allow. Use the individual tools to follow their policy.")
    task = _text(a.get("task"), "task", 8000)
    steps = _integer(a.get("max_steps", 6), "max_steps", 1, 8)
    domains = _domains(a.get("domains"))
    return _run(ctx, "Research the task using web_search and web_fetch. Search, read promising primary sources, "
                "and follow up only as necessary within the tool budget. Return findings with direct source links, "
                "disagreements and anything you could not verify. Task:\n" + task,
                [_server_tool("search", min(3, steps), domains), _server_tool("fetch", min(5, steps), domains)], steps)


DOMAINS = {"type": "array", "items": {"type": "string"}, "maxItems": 10,
           "description": "Optional public hostnames to restrict sources, e.g. ['docs.python.org']."}
TOOLS = [
    Tool("web_search", "Search the public web via a read-only OpenRouter scout. Returns a model summary with source "
         "links and reported retrieval activity, not raw search results. Pass only the specific public query; never "
         "secrets or the whole conversation.", schema({"query": {"type": "string", "maxLength": 4000},
         "max_results": {"type": "integer", "minimum": 1, "maximum": 10}, "domains": DOMAINS}, ["query"]),
         web_search, "web.search", "web"),
    Tool("web_fetch", "Read a public web page or PDF via OpenRouter and return a focused model summary with source "
         "links. This is not raw HTML, a browser session, or an asset download. Supply a public URL without credentials.",
         schema({"url": {"type": "string", "maxLength": 2048},
                 "focus": {"type": "string", "maxLength": 4000}}, ["url"]), web_fetch, "web.fetch", "web"),
    Tool("web_research", "Bounded agentic browsing: a read-only OpenRouter scout searches and fetches sources over "
         "multiple steps, returning a cited model synthesis and uncertainties. No workspace or editing tools. Send "
         "a self-contained public research task, not private briefs or credentials.", schema({
             "task": {"type": "string", "maxLength": 8000}, "domains": DOMAINS,
             "max_steps": {"type": "integer", "minimum": 1, "maximum": 8}}, ["task"]),
         web_research, "web.research", "web"),
]
