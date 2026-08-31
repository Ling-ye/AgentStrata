"""Transport-neutral state passed through one authorized application turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from chatcopilot.contracts.authorization import AuthorizationDecision, Principal
from chatcopilot.contracts.gateway import CanonicalInboundEvent, MessageSegment


class TurnStage(str, Enum):
    TRANSPORT_VERIFIED = "transport_verified"
    INTAKE_RECORDED = "intake_recorded"
    ADMISSION = "admission"
    ACTOR_ACTIVATION = "actor_activation"
    COMMAND_AUTHORIZATION = "command_authorization"
    APPROVAL_RESOLUTION = "approval_resolution"
    RESOURCE_MATERIALIZATION = "resource_materialization"
    DETERMINISTIC_SHORTCUT = "deterministic_shortcut"
    ACTOR_SESSION_MATERIALIZATION = "actor_session_materialization"
    EXECUTION = "execution"
    OUTBOUND_PERSISTED = "outbound_persisted"
    DELIVERY_OBSERVED = "delivery_observed"
    FINISH = "finish"


TURN_STAGE_ORDER = tuple(TurnStage)


class TurnDirective(str, Enum):
    CONTINUE = "continue"
    OUTBOUND = "outbound"
    FINISH = "finish"


@dataclass(frozen=True)
class StageResult:
    directive: TurnDirective = TurnDirective.CONTINUE
    code: str = ""


@dataclass
class TurnContext:
    """Mutable application state; transport edges never receive this object."""

    run_id: str
    session_id: str
    inbound: CanonicalInboundEvent
    principal: Principal | None = None
    authorization_decisions: list[AuthorizationDecision] = field(default_factory=list)
    response_segments: tuple[MessageSegment, ...] = ()
    resource_refs: tuple[Any, ...] = ()
    task_ref: Any = None
    actor_state: Any = None
    error_code: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    completed_stages: list[TurnStage] = field(default_factory=list)
    skipped_stages: list[TurnStage] = field(default_factory=list)

    def complete(self, stage: TurnStage) -> None:
        if stage in self.completed_stages or stage in self.skipped_stages:
            raise RuntimeError(f"turn stage already accounted for: {stage.value}")
        self.completed_stages.append(stage)

    def skip(self, stage: TurnStage) -> None:
        if stage in self.completed_stages or stage in self.skipped_stages:
            raise RuntimeError(f"turn stage already accounted for: {stage.value}")
        self.skipped_stages.append(stage)

    @property
    def accounted_stages(self) -> tuple[TurnStage, ...]:
        completed = set(self.completed_stages)
        skipped = set(self.skipped_stages)
        return tuple(stage for stage in TURN_STAGE_ORDER if stage in completed or stage in skipped)


__all__ = [
    "StageResult",
    "TURN_STAGE_ORDER",
    "TurnContext",
    "TurnDirective",
    "TurnStage",
]
