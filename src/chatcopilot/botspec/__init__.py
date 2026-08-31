"""BotSpec subsystem: model, loading/validation, registry, and runtime assembly."""
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import (
    BotSpec,
    ChannelsSpec,
    GatewaySpec,
    QQChannelSpec,
    ValidationIssue,
)
from chatcopilot.botspec.mcp import McpServerConfig
from chatcopilot.botspec.rag import RagSourceConfig
from chatcopilot.botspec.registry import resolve_bot_spec_path
from chatcopilot.botspec.runtime import (
    BotRuntimeContext,
    assemble_runtime_context,
    load_runtime_context,
)

__all__ = [
    "BotSpec",
    "GatewaySpec",
    "ChannelsSpec",
    "QQChannelSpec",
    "ValidationIssue",
    "McpServerConfig",
    "RagSourceConfig",
    "load_botspec",
    "validate_botspec",
    "resolve_bot_spec_path",
    "BotRuntimeContext",
    "assemble_runtime_context",
    "load_runtime_context",
]
