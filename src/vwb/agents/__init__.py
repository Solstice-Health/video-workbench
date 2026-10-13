"""Agents: the job lead (Claude Agent SDK), workers (the harness's own tool loop over GPT or
Gemini), the swarm that links them, and adapters for other hosts. Every host reaches the
toolkit through the same gateway path (vwb.tools.base.Registry.call)."""
