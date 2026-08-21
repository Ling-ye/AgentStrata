from __future__ import annotations

from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder
from chatcopilot.contracts.prompt import BotPromptProfile, PromptPlan


def prompt_plan(
    identity: str = "Test assistant",
    *,
    backend: str = "native",
    role: str = "owner",
    channel_kind: str = "private",
) -> PromptPlan:
    return PromptPlanBuilder().build(
        prompt_input(
            identity,
            backend=backend,
            role=role,
            channel_kind=channel_kind,
        )
    )


def prompt_input(
    identity: str = "Test assistant",
    *,
    backend: str = "native",
    role: str = "owner",
    channel_kind: str = "private",
) -> PromptBuildInput:
    return PromptBuildInput(
        profile=BotPromptProfile(
            identity=identity or "Test assistant",
            response_style="Return concise deterministic test responses.",
        ),
        backend=backend,
        model=None,
        role=role,
        channel_kind=channel_kind,
        session_policy="Test runtime policy.",
    )
