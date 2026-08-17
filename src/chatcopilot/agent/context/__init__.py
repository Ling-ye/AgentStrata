"""Agent 上下文工程：system prompt 装配 + AgentTask → user message 翻译 + 上下文窗口管理。"""
from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.prompt_builder import build_system_prompt
from chatcopilot.agent.context.task_framing import (
    frame_task_content,
    frame_task_message,
    validated_image_resource_receipts,
)
from chatcopilot.agent.context.token_estimator import estimate_tokens

__all__ = [
    "ContextManager",
    "build_system_prompt",
    "estimate_tokens",
    "frame_task_content",
    "frame_task_message",
    "validated_image_resource_receipts",
]
