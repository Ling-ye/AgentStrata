"""Provider-neutral contracts for the three main-agent runtimes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypedDict, runtime_checkable

from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.cancellation import CancellationProbe
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.contracts.prompt import PromptPlan
from chatcopilot.contracts.execution import CapabilitySnapshot, HostRuntimePolicy
from chatcopilot.contracts.execution import TranscriptSnapshot
from chatcopilot.contracts.model_runtime import ResolvedRuntimeRoute


RUNTIME_IDS: tuple[str, ...] = ("native", "langgraph", "codex")
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
    connected_apps: bool = True
    image_generation: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.connected_apps, bool):
            raise TypeError("Codex connected_apps must be a boolean")
        if not isinstance(self.image_generation, bool):
            raise TypeError("Codex image_generation must be a boolean")
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
class RuntimeCapabilities:
    """Capabilities registered by code, never supplied by BotSpec."""

    names: frozenset[str]
    tool_names: frozenset[str] = frozenset()

    def intersect_tools(self, allowed_tool_names: set[str] | frozenset[str]) -> "RuntimeCapabilities":
        allowed = frozenset(allowed_tool_names)
        return RuntimeCapabilities(names=self.names, tool_names=self.tool_names & allowed)


@dataclass(frozen=True)
class RuntimeSessionRef:
    """Opaque runtime-native session reference.

    Middleware may persist and compare the value, but must not parse it.
    """

    runtime_id: str
    value: str


class RuntimeSessionOptions(TypedDict, total=False):
    workspace_root: str | Path | None
    source_root: str | Path | None
    runtime_state_root: str | Path | None
    isolate_runtime_state: bool
    restore_persisted_native_session: bool
    role_hint: str
    execution_scope: ExecutionScope | None


@dataclass(frozen=True)
class RuntimeOpenRequest:
    session_id: str
    prompt_plan: PromptPlan
    route: ResolvedRuntimeRoute
    allowed_tool_names: frozenset[str] = frozenset()
    required_capabilities: frozenset[str] = frozenset({CAPABILITY_CHAT})
    caller_identity: SessionIdentity | None = None
    options: RuntimeSessionOptions = field(default_factory=RuntimeSessionOptions)
    capability_snapshot: CapabilitySnapshot = field(default_factory=CapabilitySnapshot)
    host_policy: HostRuntimePolicy = field(default_factory=HostRuntimePolicy)

    def __post_init__(self):
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


class RuntimeCapabilityError(RuntimeError):
    def __init__(self, runtime_id: str, capability: str, *, suggestion: str) -> None:
        super().__init__(
            f"runtime {runtime_id!r} does not provide capability {capability!r}; {suggestion}"
        )
        self.runtime_id = runtime_id
        self.capability = capability
        self.suggestion = suggestion
        self.error_code = "runtime_capability_missing"


def require_runtime_capabilities(
    runtime_id: str,
    capabilities: RuntimeCapabilities,
    required: frozenset[str],
) -> None:
    missing = sorted(required - capabilities.names)
    if missing:
        capability = missing[0]
        raise RuntimeCapabilityError(
            runtime_id,
            capability,
            suggestion=(
                "select an instance runtime that registers this capability in "
                "BotSpec agents.runtime"
            ),
        )


@runtime_checkable
class RuntimeAdapter(Protocol):
    @property
    def capabilities(self) -> RuntimeCapabilities: ...

    def open_session(self, request: RuntimeOpenRequest) -> RuntimeSessionRef: ...

    def stream_turn(
        self,
        session: RuntimeSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult: ...

    def close_session(self, session: RuntimeSessionRef) -> None: ...

    def discard_session(self, session: RuntimeSessionRef) -> None: ...

    def cancel(self, session: RuntimeSessionRef) -> None: ...

    def is_busy(self, session: RuntimeSessionRef) -> bool: ...

    def set_prompt_plan(self, session: RuntimeSessionRef, plan: PromptPlan) -> None: ...

    def record_exchange(self, session: RuntimeSessionRef, user_text: str, assistant_text: str) -> None: ...

    def snapshot_transcript(self, session: RuntimeSessionRef) -> TranscriptSnapshot: ...


__all__ = [
    "RUNTIME_IDS",
    "RuntimeAdapter",
    "RuntimeCapabilities",
    "RuntimeCapabilityError",
    "RuntimeOpenRequest",
    "RuntimeSessionRef",
    "RuntimeSessionOptions",
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
    "require_runtime_capabilities",
]
