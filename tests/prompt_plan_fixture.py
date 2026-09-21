from __future__ import annotations

from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder
from chatcopilot.contracts.prompt import BotPromptProfile, PromptPlan
from chatcopilot.contracts.model_runtime import ResolvedRuntimeRoute
from chatcopilot.core.config import LLMConfig


def runtime_route(
    runtime_id: str = "native",
    config: LLMConfig | None = None,
) -> ResolvedRuntimeRoute:
    if config is None:
        config = (
            LLMConfig(
                provider="openai",
                api="openai_responses",
                model="gpt-test",
                api_key="fixture",
            )
            if runtime_id == "codex"
            else LLMConfig(api_key="fixture")
        )
    return ResolvedRuntimeRoute(
        runtime_id,  # type: ignore[arg-type]
        config.model_route(),
        900 if runtime_id == "codex" else None,
    )


def prompt_plan(
    identity: str = "Test assistant",
    *,
    runtime_id: str = "native",
    role: str = "owner",
    channel_kind: str = "private",
) -> PromptPlan:
    return PromptPlanBuilder().build(
        prompt_input(
            identity,
            runtime_id=runtime_id,
            role=role,
            channel_kind=channel_kind,
        )
    )


def prompt_input(
    identity: str = "Test assistant",
    *,
    runtime_id: str = "native",
    role: str = "owner",
    channel_kind: str = "private",
) -> PromptBuildInput:
    return PromptBuildInput(
        profile=BotPromptProfile(
            identity=identity or "Test assistant",
            response_style="Return concise deterministic test responses.",
        ),
        runtime_id=runtime_id,
        model=None,
        role=role,
        channel_kind=channel_kind,
        session_policy="Test runtime policy.",
    )
