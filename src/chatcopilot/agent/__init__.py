"""AgentStrata agent runtime：固定协议契约 + LLM ↔ 工具循环。

agent 层只关心：LLM 客户端、工具调度、上下文工程、技能/记忆/RAG 接入。它通过
``AgentTask``（任务文本 + 资源句柄）与上层交互，通过 ``AgentEvent`` 流式回报
中间过程，最后返回 ``AgentResult``。agent 内部不感知 chat 平台、协议帧、用户
角色、调试模式、附件流程等概念。
"""
from chatcopilot.contracts.agent import (
    AgentEvent,
    AgentResult,
    AgentTask,
    EventSink,
    FinalText,
    ResourceRef,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnError,
)
from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.runtime import AgentRuntime, build_agent_runtime
from chatcopilot.agent.session import AgentSession, ToolPayloadFilter
from chatcopilot.agent.session_protocol import AgentSessionProtocol

__all__ = [
    "AgentEvent",
    "AgentResult",
    "AgentRuntime",
    "AgentSession",
    "AgentSessionProtocol",
    "AgentTask",
    "EventSink",
    "FinalText",
    "LangGraphAgentSession",
    "ResourceRef",
    "TextDelta",
    "ToolFinished",
    "ToolPayloadFilter",
    "ToolStarted",
    "TurnError",
    "build_agent_runtime",
]
