"""Canary lifecycle state and fail-closed quarantine decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import CanaryStateError


class CanaryPhase(str, Enum):
    INITIAL = "initial"
    PREPARED = "prepared"
    BASELINE_VERIFIED = "baseline_verified"
    AGENT_TURN_COMPLETED = "agent_turn_completed"
    LIFECYCLE_REQUESTED = "lifecycle_requested"
    CANDIDATE_VALIDATED = "candidate_validated"
    ACTIVATED = "activated"
    RESTART_OBSERVED = "restart_observed"
    CANDIDATE_BEHAVIOR_VERIFIED = "candidate_behavior_verified"
    RESTORING = "restoring"
    BASELINE_RESTORED = "baseline_restored"
    CLEANUP_COMPLETED = "cleanup_completed"
    QUARANTINED = "quarantined"


_SUCCESS_TRANSITIONS: dict[CanaryPhase, frozenset[CanaryPhase]] = {
    CanaryPhase.INITIAL: frozenset({CanaryPhase.PREPARED}),
    CanaryPhase.PREPARED: frozenset(
        {CanaryPhase.BASELINE_VERIFIED, CanaryPhase.RESTORING}
    ),
    CanaryPhase.BASELINE_VERIFIED: frozenset(
        {CanaryPhase.AGENT_TURN_COMPLETED, CanaryPhase.RESTORING}
    ),
    CanaryPhase.AGENT_TURN_COMPLETED: frozenset(
        {CanaryPhase.LIFECYCLE_REQUESTED, CanaryPhase.RESTORING}
    ),
    CanaryPhase.LIFECYCLE_REQUESTED: frozenset(
        {CanaryPhase.CANDIDATE_VALIDATED, CanaryPhase.RESTORING}
    ),
    CanaryPhase.CANDIDATE_VALIDATED: frozenset(
        {CanaryPhase.ACTIVATED, CanaryPhase.RESTORING}
    ),
    CanaryPhase.ACTIVATED: frozenset({CanaryPhase.RESTART_OBSERVED, CanaryPhase.RESTORING}),
    CanaryPhase.RESTART_OBSERVED: frozenset(
        {CanaryPhase.CANDIDATE_BEHAVIOR_VERIFIED, CanaryPhase.RESTORING}
    ),
    CanaryPhase.CANDIDATE_BEHAVIOR_VERIFIED: frozenset({CanaryPhase.RESTORING}),
    CanaryPhase.RESTORING: frozenset({CanaryPhase.BASELINE_RESTORED}),
    CanaryPhase.BASELINE_RESTORED: frozenset({CanaryPhase.CLEANUP_COMPLETED}),
    CanaryPhase.CLEANUP_COMPLETED: frozenset(),
    CanaryPhase.QUARANTINED: frozenset(),
}


class CanaryStateMachine:
    def __init__(self, phase: CanaryPhase = CanaryPhase.INITIAL) -> None:
        self._phase = phase

    @property
    def phase(self) -> CanaryPhase:
        return self._phase

    def advance(self, next_phase: CanaryPhase) -> CanaryPhase:
        if next_phase == CanaryPhase.QUARANTINED:
            if self._phase in {CanaryPhase.CLEANUP_COMPLETED, CanaryPhase.QUARANTINED}:
                raise CanaryStateError(f"cannot quarantine terminal phase {self._phase.value}")
            self._phase = next_phase
            return self._phase
        if next_phase not in _SUCCESS_TRANSITIONS[self._phase]:
            raise CanaryStateError(
                f"invalid Canary transition: {self._phase.value} -> {next_phase.value}"
            )
        self._phase = next_phase
        return self._phase


class QuarantineScope(str, Enum):
    NONE = "none"
    TARGET = "target"
    SUBSYSTEM = "subsystem"


@dataclass(frozen=True, slots=True)
class QuarantineDecision:
    scope: QuarantineScope
    reasons: tuple[str, ...]

    @property
    def quarantined(self) -> bool:
        return self.scope != QuarantineScope.NONE


def decide_quarantine(
    *,
    mutation_started: bool,
    production_unchanged: bool | None,
    paths_contained: bool | None,
    observer_identity_proven: bool | None,
    active_generation_known: bool | None,
    baseline_restored: bool | None,
    unit_stopped: bool | None,
) -> QuarantineDecision:
    """Return a conservative containment decision after failure or cancellation.

    ``None`` means unknown, not "not applicable". Unknown production or path
    containment escalates to a subsystem block. Once mutation started, every
    observer/generation/restore/process fact must be positively proven.
    """

    subsystem_reasons: list[str] = []
    if production_unchanged is not True:
        subsystem_reasons.append("production_unchanged_not_proven")
    if paths_contained is not True:
        subsystem_reasons.append("canary_path_containment_not_proven")
    if subsystem_reasons:
        return QuarantineDecision(QuarantineScope.SUBSYSTEM, tuple(subsystem_reasons))

    if not mutation_started:
        if unit_stopped is True:
            return QuarantineDecision(QuarantineScope.NONE, ())
        return QuarantineDecision(
            QuarantineScope.TARGET,
            ("pre_activation_unit_stop_not_proven",),
        )

    target_reasons: list[str] = []
    if observer_identity_proven is not True:
        target_reasons.append("observer_identity_not_proven")
    if active_generation_known is not True:
        target_reasons.append("active_generation_not_proven")
    if baseline_restored is not True:
        target_reasons.append("baseline_restore_not_proven")
    if unit_stopped is not True:
        target_reasons.append("canary_unit_stop_not_proven")
    if target_reasons:
        return QuarantineDecision(QuarantineScope.TARGET, tuple(target_reasons))
    return QuarantineDecision(QuarantineScope.NONE, ())


__all__ = [
    "CanaryPhase",
    "CanaryStateMachine",
    "QuarantineDecision",
    "QuarantineScope",
    "decide_quarantine",
]
