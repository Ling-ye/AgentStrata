from __future__ import annotations

import importlib
from pathlib import Path

import pytest


RETIRED = (
    "agent.config", "agent.concurrency", "agent.llm_client", "agent.protocol",
    "botspec.mcp_catalog", "core.workspace", "agent.subagents.presets",
    "agent.tools.builtin.mcp_tools", "middleware.runtime.workspace",
    *("middleware.runtime.workspace." + part for part in (
        "cleanup", "identity", "inventory", "model", "resolver", "service",
    )),
)


@pytest.mark.parametrize("suffix", RETIRED)
def test_l01_import_path_no_longer_loads(suffix: str) -> None:
    module = "chatcopilot." + suffix
    with pytest.raises(ModuleNotFoundError) as raised:
        importlib.import_module(module)
    assert module == raised.value.name or module.startswith(raised.value.name + ".")


def test_current_exports_remain_usable_without_runtime_materialization() -> None:
    from chatcopilot.contracts.agent import AgentResult, AgentTask
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.concurrency import FileTokenLimiter
    from chatcopilot.core.llm_client import LLMClient
    from chatcopilot.core.mcp_catalog import load_mcp_catalog
    from chatcopilot.core.workspace_runtime import Workspace
    from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS
    from chatcopilot.external_tools.mcp_admin.tools import TOOLS

    assert ChatConfig().llm is not None
    assert callable(LLMClient) and callable(FileTokenLimiter)
    assert callable(load_mcp_catalog)
    assert AgentTask("input").text == "input"
    assert AgentResult("result", "end_turn").final_text == "result"
    assert Workspace(root=Path("example"), chat_kind="p2p", chat_id=None, user_id="actor").root == Path("example")
    assert "developer" in BUILTIN_SUBAGENTS
    assert any(tool.name == "list_mcp_servers" for tool in TOOLS)
