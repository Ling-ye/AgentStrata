"""Complete ACP construction for isolated tests, without model or host discovery."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from chatcopilot.botspec.model import BotSpec, PlatformSpec, PromptSpec, ToolSpec
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.core.config import ChatConfig, LLMConfig
from chatcopilot.middleware.acp.server import AcpChatAgent


def acp_runtime(
    *,
    platform_type: str = "feishu",
    tool_features: tuple[str, ...] | None = None,
    **overrides,
) -> BotRuntimeContext:
    features = tool_features if tool_features is not None else (
        ("chat.file_uploads", "chat.private_workspace") if platform_type == "feishu" else ()
    )
    spec = BotSpec(
        id="test-acp", display_name="Test ACP",
        platform=PlatformSpec(platform_type, "cc-connect"),
        prompts=PromptSpec(schema_version=2, identity="identity.md", response_style="style.md"),
        source_path=Path(__file__), tools=ToolSpec(features=features),
    )
    runtime = BotRuntimeContext(
        spec=spec, bot_id=spec.id, instance_id=spec.id, display_name=spec.display_name,
        platform_type=platform_type, platform_adapter=spec.platform.adapter,
        prompt_profile=BotPromptProfile("Test assistant", "Answer clearly"),
        capability_policies=(), tool_packs=(), tool_features=features, exclude_tools=(),
        workspace_root=None, log_dir=None, source_path=spec.source_path,
    )
    return replace(runtime, **overrides)


def make_acp_agent(runtime: BotRuntimeContext | None = None) -> AcpChatAgent:
    config = ChatConfig(llm=LLMConfig(model="gpt-4o-mini"))
    with (
        patch("chatcopilot.middleware.acp.server.load_config", return_value=config),
        patch("chatcopilot.middleware.acp.server.project_agent_runtime", return_value=object()),
    ):
        return AcpChatAgent(runtime=runtime or acp_runtime())
