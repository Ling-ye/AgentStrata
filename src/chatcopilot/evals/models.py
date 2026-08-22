"""Structured contracts for AgentStrata evaluations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SuiteKind = Literal[
    "product", "knowledge", "reasoning", "code", "agent", "tool", "web", "context", "safety"
]
RunStatus = Literal["passed", "failed", "skipped", "error", "unavailable"]
EvalRunStatus = RunStatus | Literal["running"]
SuiteStatus = Literal["implemented", "planned"]
DriverId = Literal[
    "agent_isolated",
    "agent_configured",
    "acp_scenario",
    "qq_message_flow",
    "direct_llm",
    "dry_run",
]
EvaluationTrack = Literal["agent", "qq_message_flow"]


@dataclass(frozen=True)
class BenchmarkStandard:
    """Metadata for a benchmark that can be manually enabled."""

    suite_id: str
    name: str
    kind: SuiteKind
    value: str
    recommendation: str
    cadence: str
    requires_bot: bool = True
    requires_external_data: bool = False
    setup_hint: str = ""
    official_url: str = ""


@dataclass(frozen=True)
class ManifestFile:
    """Digest-pinned package resource referenced by a suite manifest."""

    path: str
    role: Literal["cases", "fixture"]
    media_type: str
    sha256: str
    resource_id: str = ""


@dataclass(frozen=True)
class ManifestOption:
    """One strictly declared, UI-safe suite option."""

    name: str
    type: Literal["boolean", "integer", "string", "enum"]
    label: str
    default: bool | int | str | None = None
    required: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class SuitePreset:
    """Named deterministic selection of case identifiers."""

    preset_id: str
    case_ids: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class SuiteManifest:
    """Strict, package-owned declaration for one evaluation suite.

    The manifest intentionally contains metadata and trusted identifiers only.
    Executable behavior is resolved through the static plugin catalog.
    """

    schema: int
    suite_id: str
    version: str
    name: str
    kind: SuiteKind
    status: SuiteStatus
    value: str
    recommendation: str
    cadence: str
    track: EvaluationTrack | str = ""
    plugin_id: str = ""
    driver_id: DriverId | str = ""
    requires_bot: bool = True
    requires_external_data: bool = False
    prepare_supported: bool = False
    setup_hint: str = ""
    official_url: str = ""
    files: tuple[ManifestFile, ...] = ()
    options: tuple[ManifestOption, ...] = ()
    presets: tuple[SuitePreset, ...] = ()
    default_preset: str = ""

    def to_standard(self) -> BenchmarkStandard:
        """Project this richer contract onto the legacy public facade."""

        return BenchmarkStandard(
            suite_id=self.suite_id,
            name=self.name,
            kind=self.kind,
            value=self.value,
            recommendation=self.recommendation,
            cadence=self.cadence,
            requires_bot=self.requires_bot,
            requires_external_data=self.requires_external_data,
            setup_hint=self.setup_hint,
            official_url=self.official_url,
        )


@dataclass(frozen=True)
class EvalCaseResource:
    """Digest-pinned resource reference exposed to one selected case."""

    resource_id: str
    path: str
    media_type: str
    sha256: str


@dataclass(frozen=True)
class EvalCaseTurn:
    """One declared input turn; outputs are observations, never Case YAML."""

    text: str
    resources: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalCaseRequirements:
    """Preflight requirements expressed without secret values or endpoints."""

    features: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    tool_packs: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    env_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalCasePolicy:
    """Core-enforced execution and result policy for one Case."""

    side_effect: Literal[
        "none", "isolated_read", "isolated_write", "external_read", "external_write"
    ] = "none"
    network: Literal["disabled", "loopback", "configured"] = "disabled"
    timeout_seconds: float = 120.0
    required_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalCaseAssertion:
    """Trusted verifier identifier and declarative expected behavior."""

    kind: Literal["trusted_verifier"]
    assertion_id: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalCaseDefinition:
    """Versioned declarative Case contract loaded by a trusted plugin."""

    schema: str
    case_id: str
    version: int
    capability: str
    plugin_id: str
    driver_id: DriverId | str
    turns: tuple[EvalCaseTurn, ...]
    requirements: EvalCaseRequirements
    policy: EvalCasePolicy
    assertions: tuple[EvalCaseAssertion, ...]
    judge_mode: Literal["all", "any"] = "all"
    presets: tuple[str, ...] = ()
    severity: Literal["required", "critical", "observational"] = "required"
    resources: tuple[EvalCaseResource, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalCase:
    """One evaluation task."""

    case_id: str
    input: str
    category: str
    expected_behavior: str
    must_have: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    context: str = ""
    rubric: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeResult:
    """Structured scoring output for a case."""

    score: float
    max_score: float
    passed: bool
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalCaseResult:
    """Execution and judgment result for one case."""

    case_id: str
    suite_id: str
    status: RunStatus
    score: float = 0.0
    max_score: float = 1.0
    final_text: str = ""
    stop_reason: str = ""
    duration_seconds: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    events: tuple[dict[str, Any], ...] = ()
    judge: JudgeResult | None = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalRunResult:
    """Aggregated result for a suite run."""

    suite_id: str
    bot: str | None
    status: EvalRunStatus
    started_at: str
    duration_seconds: float
    cases: tuple[EvalCaseResult, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class TrialObservation:
    """Untrusted trial output normalized before Core creates authoritative evidence."""

    final_text: str = ""
    stop_reason: str = ""
    events: tuple[dict[str, Any], ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()
    produced_resources: tuple[dict[str, Any], ...] = ()
    post_state: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    model_timing: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    structured_error: dict[str, Any] | None = None


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses recursively into JSON-serializable values."""

    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(raw) for key, raw in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(raw) for key, raw in value.items()}
    return value
