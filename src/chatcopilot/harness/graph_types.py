"""Serializable workflow state; execution capabilities stay outside checkpoints."""

from __future__ import annotations

from typing import Any, Callable, Protocol, TypedDict

GRAPH_VERSION = 1


class GraphState(TypedDict, total=False):
    identity: str
    version: int
    attempt: int
    phase: str
    route: str
    context_ref: dict[str, Any] | None
    submission_ref: dict[str, Any] | None
    error_ref: dict[str, Any] | None
    deferred_error_ref: dict[str, Any] | None
    pending_roles: list[str]
    outcome: str
    replaying: bool
    sequence: int
    child_id: str
    kind: str
    preflighted: bool


class AcceptanceOperations(Protocol):
    def node(self, name: str) -> Callable[[GraphState], GraphState]: ...


def acceptance_route(state: GraphState) -> str:
    return "done" if state.get("error_ref") or state.get("outcome") else "next"
