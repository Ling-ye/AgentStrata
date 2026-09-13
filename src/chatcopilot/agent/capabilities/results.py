"""Bind the result reader to the same live session as the tool executor."""
from __future__ import annotations

from chatcopilot.agent.capabilities.assembly import SessionCapabilityContext
from chatcopilot.agent.tools.result_reader import result_reader_provider
from chatcopilot.contracts.tool_packs import ToolProvider


def build_provider(context: SessionCapabilityContext) -> ToolProvider | None:
    if context.result_store is None or not any(
        tool.metadata.get("result_content_field")
        and (context.permission_filter is None or context.permission_filter(tool) is None)
        for tool in context.base_tools
    ):
        return None
    return result_reader_provider(context.result_store)
