"""Typed per-turn identity, capability and private runtime binding contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import asdict, fields
from typing import Literal, Any

from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.contracts.model_runtime import ModelSelection, digest, freeze_json


@dataclass(frozen=True)
class TraceContext:
    trace_id: str = ""
    parent_span_id: str | None = None
    depth: int = 0


@dataclass(frozen=True)
class TurnExecutionContext:
    execution_id: str = ""
    session_id: str = ""
    actor_ref: str = ""
    origin: Literal["gateway", "evaluation", "background", "legacy"] = "legacy"
    turn_index: int = 0
    trace: TraceContext = field(default_factory=TraceContext)
    model_selection: ModelSelection | None = None

    def __post_init__(self) -> None:
        if self.origin not in {"gateway", "evaluation", "background", "legacy"}:
            raise ValueError("invalid execution origin")
        if type(self.turn_index) is not int or self.turn_index < 0:
            raise ValueError("turn index must be non-negative")


@dataclass(frozen=True)
class HostRuntimePolicy:
    scope: ExecutionScope | None = None
    network_access: bool = False
    native_capabilities: frozenset[str] = frozenset()
    extension_grants: tuple[str, ...] = ()
    interactions_enabled: bool = False
    revision: str = "runtime-access-v3"

    def __post_init__(self):
        object.__setattr__(self, "native_capabilities", frozenset(self.native_capabilities))
        object.__setattr__(self, "extension_grants", tuple(self.extension_grants))

    @property
    def fingerprint(self):
        scope: dict[str, Any] | None = None
        if self.scope is not None:
            scope = {
                key: sorted(str(path) for path in getattr(self.scope, key))
                for key in (
                    "readable_roots",
                    "writable_roots",
                    "project_roots",
                    "protected_roots",
                    "hidden_roots",
                )
            }
            scope.update(
                native_write=self.scope.native_write,
                policy_version=self.scope.policy_version,
                command_timeouts=asdict(self.scope.command_timeouts),
            )
        return digest(
            {
                "scope": scope,
                "network_access": self.network_access,
                "native_capabilities": sorted(self.native_capabilities),
                "extension_grants": sorted(self.extension_grants),
                "interactions_enabled": self.interactions_enabled,
                "revision": self.revision,
            }
        )


@dataclass(frozen=True)
class Capability:
    capability_id: str
    execution_owner: Literal["host", "codex"]
    host_tool_name: str | None = None
    native_capability: str | None = None
    source_ref: str = ""
    configured: bool = True
    supported: bool | None = None
    authorized: bool = False
    availability: Literal["available", "unavailable", "unobserved"] = "unobserved"
    loading: Literal["direct", "deferred"] = "direct"
    reason_code: str = ""
    schema_digest: str = ""

    def __post_init__(self) -> None:
        if self.execution_owner not in {"host", "codex"}:
            raise ValueError("invalid capability owner")
        if (self.execution_owner == "host") != bool(self.host_tool_name):
            raise ValueError("host capability requires exactly one host tool")
        if (self.execution_owner == "codex") != bool(self.native_capability):
            raise ValueError("native capability requires exactly one native identity")


@dataclass(frozen=True)
class CapabilitySnapshot:
    entries: tuple[Capability, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if len({entry.capability_id for entry in self.entries}) != len(self.entries):
            raise ValueError("duplicate capability identity")

    @property
    def fingerprint(self) -> str:
        return digest(
            [
                {
                    key: getattr(entry, key)
                    for key in (
                        "capability_id",
                        "execution_owner",
                        "host_tool_name",
                        "native_capability",
                        "source_ref",
                        "configured",
                        "authorized",
                        "loading",
                        "schema_digest",
                    )
                }
                for entry in sorted(self.entries, key=lambda value: value.capability_id)
            ]
        )

    def to_payload(self):
        return {"digest": self.fingerprint, "entries": [asdict(entry) for entry in self.entries]}


@dataclass(frozen=True)
class RuntimeFailure:
    code: str
    stage: Literal[
        "configuration",
        "authentication",
        "protocol",
        "model",
        "tool",
        "interaction",
        "cancellation",
    ]
    summary: str
    user_action_required: bool = False
    resubmission_allowed: bool = False


@dataclass(frozen=True)
class RuntimeSessionBinding:
    binding_id: str
    actor_key: str
    runtime_id: str
    native_thread_id: str
    auth_identity_epoch: int
    scope_digest: str
    capability_digest: str
    runtime_config_digest: str
    resume_policy: Literal["live_only", "restartable"] = "live_only"
    status: Literal["active", "invalidated", "archived"] = "active"

    def __post_init__(self):
        if self.runtime_id not in {"native", "langgraph", "codex"} or self.resume_policy not in {
            "live_only",
            "restartable",
        }:
            raise ValueError("invalid runtime binding")
        if (
            self.status not in {"active", "invalidated", "archived"}
            or type(self.auth_identity_epoch) is not int
        ):
            raise ValueError("invalid runtime binding state")

    def to_payload(self):
        return {"schema_version": 4, **asdict(self)}

    @classmethod
    def from_payload(cls, payload):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", *(f.name for f in fields(cls))}
            or payload["schema_version"] != 4
        ):
            raise ValueError("runtime binding requires explicit migration")
        return cls(**{key: value for key, value in payload.items() if key != "schema_version"})


@dataclass(frozen=True)
class ResponseIntegrity:
    ok: bool
    issues: tuple[str, ...] = ()
    evidence_digest: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class TranscriptSnapshot:
    messages: tuple[dict, ...]
    coverage: Literal["host_history", "adapter_visible"]

    def __post_init__(self):
        object.__setattr__(
            self, "messages", tuple(freeze_json(message) for message in self.messages)
        )
