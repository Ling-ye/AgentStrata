"""Provider-neutral contracts for the three main-agent backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.identity import SessionIdentity


CAPABILITY_CHAT = "chat"
CAPABILITY_TOOLS = "tools"
CAPABILITY_NATIVE_RESUME = "native_resume"
CAPABILITY_REPOSITORY_MUTATION = "repository_mutation"
CODEX_ACCESS_WORKSPACE = "workspace"
CODEX_ACCESS_WORKTREE = "worktree"
CODEX_ACCESS_MODES = frozenset({CODEX_ACCESS_WORKSPACE, CODEX_ACCESS_WORKTREE})


@dataclass(frozen=True)
class CodexMainSessionPolicy:
    """Role-level access modes for the Codex main-agent backend."""

    owner_access: str = CODEX_ACCESS_WORKSPACE
    member_access: str = CODEX_ACCESS_WORKSPACE

    def access_for_role(self, role_hint: str) -> str:
        return (
            self.owner_access
            if str(role_hint).strip().lower() == "owner"
            else self.member_access
        )


@dataclass(frozen=True)
class BackendCapabilities:
    """Capabilities registered by code, never supplied by BotSpec."""

    names: frozenset[str]
    tool_names: frozenset[str] = frozenset()

    def intersect_tools(self, allowed_tool_names: set[str] | frozenset[str]) -> "BackendCapabilities":
        allowed = frozenset(allowed_tool_names)
        return BackendCapabilities(names=self.names, tool_names=self.tool_names & allowed)


@dataclass(frozen=True)
class BackendSessionRef:
    """Opaque backend-native session reference.

    Middleware may persist and compare the value, but must not parse it.
    """

    backend: str
    value: str


@dataclass(frozen=True)
class BackendOpenRequest:
    session_id: str
    system_baseline: str
    allowed_tool_names: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset({CAPABILITY_CHAT})
    caller_identity: SessionIdentity | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


class BackendCapabilityError(RuntimeError):
    def __init__(self, backend: str, capability: str, *, suggestion: str) -> None:
        super().__init__(
            f"backend {backend!r} does not provide capability {capability!r}; {suggestion}"
        )
        self.backend = backend
        self.capability = capability
        self.suggestion = suggestion
        self.error_code = "backend_capability_missing"


def require_backend_capabilities(
    backend: str,
    capabilities: BackendCapabilities,
    required: frozenset[str],
) -> None:
    missing = sorted(required - capabilities.names)
    if missing:
        capability = missing[0]
        raise BackendCapabilityError(
            backend,
            capability,
            suggestion=(
                "select an instance backend that registers this capability in "
                "BotSpec agents.backend"
            ),
        )


@runtime_checkable
class AgentBackend(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...

    def open_session(self, request: BackendOpenRequest) -> BackendSessionRef: ...

    def stream_turn(
        self,
        session: BackendSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
    ) -> AgentResult: ...

    def close_session(self, session: BackendSessionRef) -> None: ...


__all__ = [
    "AgentBackend",
    "BackendCapabilities",
    "BackendCapabilityError",
    "BackendOpenRequest",
    "BackendSessionRef",
    "CAPABILITY_CHAT",
    "CAPABILITY_NATIVE_RESUME",
    "CAPABILITY_REPOSITORY_MUTATION",
    "CAPABILITY_TOOLS",
    "CODEX_ACCESS_MODES",
    "CODEX_ACCESS_WORKSPACE",
    "CODEX_ACCESS_WORKTREE",
    "CodexMainSessionPolicy",
    "require_backend_capabilities",
]
