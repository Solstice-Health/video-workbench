"""The toolkit: every typed tool agents can call, in one registry."""

from __future__ import annotations

from vwb.tools.base import Registry, Tool, ToolContext, ToolResult


def build_registry() -> Registry:
    from vwb.tools import (
        browser_tools,
        canvas_tools,
        edit_tools,
        file_tools,
        gen_tools,
        index_tools,
        job_tools,
        media_tools,
        people_tools,
        web_tools,
    )
    tools: list[Tool] = []
    for module in (job_tools, file_tools, index_tools, media_tools, edit_tools, gen_tools, canvas_tools, people_tools,
                   web_tools, browser_tools):
        tools.extend(module.TOOLS)
    return Registry(tools)


__all__ = ["Registry", "Tool", "ToolContext", "ToolResult", "build_registry"]
