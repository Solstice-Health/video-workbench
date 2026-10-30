"""The toolkit: every typed tool agents can call, in one registry."""

from __future__ import annotations

from vwb.tools.base import Registry, Tool, ToolContext, ToolResult


def build_registry() -> Registry:
    from vwb.tools import (
        after_effects_tools,
        agent_tools,
        artifact_tools,
        attachment_tools,
        browser_tools,
        canvas_tools,
        code_tools,
        design_context_tools,
        edit_tools,
        file_tools,
        gen_tools,
        index_tools,
        inspection_tools,
        integration_tools,
        job_tools,
        media_tools,
        model_tools,
        pdf_tools,
        people_tools,
        project_memory_tools,
        qc_chat_tools,
        sandbox_tools,
        web_tools,
    )
    tools: list[Tool] = []
    for module in (job_tools, file_tools, index_tools, media_tools, edit_tools, gen_tools, canvas_tools, people_tools,
                   web_tools, browser_tools, integration_tools, agent_tools, artifact_tools,
                   inspection_tools, sandbox_tools, attachment_tools, pdf_tools, after_effects_tools, model_tools, code_tools,
                   qc_chat_tools, design_context_tools, project_memory_tools):
        tools.extend(module.TOOLS)
    return Registry(tools)


__all__ = ["Registry", "Tool", "ToolContext", "ToolResult", "build_registry"]
