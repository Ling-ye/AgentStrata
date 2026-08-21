"""Agent context framing, prompt planning, and window management."""
from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.prompt_plan import (
    PromptBuildInput,
    PromptPlanBuilder,
    render_codex_prompt,
    render_native_prefix,
    render_receipt,
)
from chatcopilot.agent.context.task_framing import (
    frame_task_content,
    frame_task_message,
    validated_image_resource_receipts,
)
from chatcopilot.agent.context.token_estimator import estimate_tokens

__all__ = [
    "ContextManager",
    "PromptBuildInput",
    "PromptPlanBuilder",
    "estimate_tokens",
    "frame_task_content",
    "frame_task_message",
    "render_codex_prompt",
    "render_native_prefix",
    "render_receipt",
    "validated_image_resource_receipts",
]
