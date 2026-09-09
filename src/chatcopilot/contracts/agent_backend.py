"""Provider-neutral contracts for the three main-agent backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.cancellation import CancellationProbe
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.contracts.prompt import PromptPlan


AGENT_BACKEND_IDS: tuple[str, ...] = ("native", "langgraph", "codex")
CAPABILITY_CHAT = "chat"
CAPABILITY_TOOLS = "tools"
CAPABILITY_NATIVE_RESUME = "native_resume"
CAPABILITY_REPOSITORY_MUTATION = "repository_mutation"
CODEX_ACCESS_WORKSPACE = "workspace"
CODEX_ACCESS_WORKTREE = "worktree"
CODEX_ACCESS_MODES = frozenset({CODEX_ACCESS_WORKSPACE, CODEX_ACCESS_WORKTREE})
CODEX_COMMAND_SANDBOX_MODES = frozenset({"read-only", "workspace-write"})
CODEX_WEB_SEARCH_MODES = frozenset({"disabled", "live"})


@dataclass(frozen=True)
class CodexMainSessionPolicy:
    """Code-owned access and command confinement for the Codex main agent.

    Production resources arrive through the host-bound ExecutionScope. Evaluation
    may explicitly restrict its own isolated command execution.
    """

    network_access: bool = True
    web_search_mode: str = "live"
    sandbox_mode: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.network_access, bool):
            raise TypeError("Codex command network_access must be a boolean")
        if self.web_search_mode not in CODEX_WEB_SEARCH_MODES:
            raise ValueError(
                "Codex command web_search_mode must be one of: "
                + ", ".join(sorted(CODEX_WEB_SEARCH_MODES))
            )
        if (
            self.sandbox_mode is not None
            and self.sandbox_mode not in CODEX_COMMAND_SANDBOX_MODES
        ):
            raise ValueError(
                "Codex command sandbox_mode must be one of: "
                + ", ".join(sorted(CODEX_COMMAND_SANDBOX_MODES))
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


class BackendSessionOptions(TypedDict, total=False):
    workspace_root: str | Path | None
    source_root: str | Path | None
    backend_state_root: str | Path | None
    isolate_backend_state: bool
    restore_persisted_native_session: bool
    role_hint: str
    execution_scope: ExecutionScope | None


@dataclass(frozen=True)
class BackendOpenRequest:
    session_id: str
    prompt_plan: PromptPlan
    allowed_tool_names: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset({CAPABILITY_CHAT})
    caller_identity: SessionIdentity | None = None
    options: BackendSessionOptions = field(default_factory=BackendSessionOptions)


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
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult: ...

    def close_session(self, session: BackendSessionRef) -> None: ...


__all__ = [
    "AGENT_BACKEND_IDS",
    "AgentBackend",
    "BackendCapabilities",
    "BackendCapabilityError",
    "BackendOpenRequest",
    "BackendSessionRef",
    "BackendSessionOptions",
    "CAPABILITY_CHAT",
    "CAPABILITY_NATIVE_RESUME",
    "CAPABILITY_REPOSITORY_MUTATION",
    "CAPABILITY_TOOLS",
    "CODEX_ACCESS_MODES",
    "CODEX_ACCESS_WORKSPACE",
    "CODEX_ACCESS_WORKTREE",
    "CODEX_COMMAND_SANDBOX_MODES",
    "CODEX_WEB_SEARCH_MODES",
    "CodexMainSessionPolicy",
    "require_backend_capabilities",
]
