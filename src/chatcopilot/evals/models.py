"""Structured contracts for AgentStrata evaluations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from contextlib import AbstractContextManager
from typing import Any, Callable, Literal, Mapping, TypeAlias

SuiteKind = Literal[
    "product", "knowledge", "reasoning", "code", "agent", "tool", "web", "context", "safety"
]
RunStatus = Literal["passed", "failed", "skipped", "error", "unavailable"]
EvalRunStatus = RunStatus | Literal["running"]
SuiteStatus = Literal["implemented", "planned", "retired"]
DriverId = Literal[
    "agent_isolated",
    "agent_configured",
    "acp_scenario",
    "qq_message_flow",
    "direct_llm",
    "dry_run",
]
EvaluationTrack = Literal["agent", "qq_message_flow"]
EvaluationSubject = Literal["model", "agent", "system"]


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

    execution_scope: str = ""
    source_type: str = "project"
    purpose: str = "engineering_regression"
    data_version: str = ""
    split: str = ""
    coverage: str = ""
    target_scope: str = ""
    native_method: str = "Suite scorer"
    scorer_origin: str = "project_adapter"
    scorer_version: str = "1"
    subject_type: EvaluationSubject | str = ""
    capability_tags: tuple[str, ...] = ()

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
    quality: dict[str, Any] = field(default_factory=dict)
    presets: tuple[str, ...] = ()
    severity: Literal["required", "critical", "observational"] = "required"
    resources: tuple[EvalCaseResource, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    capability_tags: tuple[str, ...] = ()
    scenario_id: str = ""
    scenario_params: dict[str, Any] = field(default_factory=dict)


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
    capability_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileCase:
    suite_id: str
    case_id: str
    dimension: str
    case: EvalCase

    @property
    def ref(self) -> str:
        return f"{self.suite_id}:{self.case_id}"


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
    error: EvaluationError | None = None
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


RESULT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class CaseExpectation:
    """Scorer-only declaration, frozen before execution; never an Agent input."""

    reference_answer: Any = None
    behavior: str = ""
    checks: tuple[str, ...] = ()
    source: str = "case_definition"


@dataclass(frozen=True)
class EvaluationError:
    stage: str
    code: str
    message: str

    @property
    def fatal(self) -> bool:
        return self.code in {
            "result_contract_error", "protocol_error", "storage_error",
            "artifact_integrity_error", "cleanup_error",
        }


@dataclass(frozen=True)
class ExecutionEvidence:
    final_text: str = ""
    stop_reason: str = ""
    events: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    total_seconds: float | None = None


@dataclass(frozen=True)
class Assessment:
    judge: JudgeResult | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float | None = None


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


@dataclass(frozen=True)
class AssertionOutcome:
    """One deterministic verifier result, independent of verifier modules."""
    passed: bool
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)


EvaluationKind = Literal["comparison", "suite"]
EvaluationStatus = Literal[
    "queued",
    "running",
    "completed",
    "partial",
    "cancelled",
    "interrupted",
    "error",
]
TrialOutcome = Literal["passed", "failed", "skipped", "error"]
TargetExecutor = Literal[
    "direct_llm",
    "agent_configured",
    "agent_isolated",
    "acp_scenario",
    "qq_message_flow",
    "dry_run",
]
ComparisonPreset = Literal["quick", "standard", "custom"]

@dataclass(frozen=True)
class EvaluationTarget:
    """Resolved and fingerprinted execution lane."""

    target_id: str
    label: str
    executor: TargetExecutor
    backend: str
    model: str
    reasoning_effort: str
    fingerprint: str
    config_fingerprint: str = ""


@dataclass(frozen=True)
class ComparisonEvaluationRequest:
    """Resolved request for a versioned Profile comparison."""

    evaluation_id: str
    kind: Literal["comparison"]
    bot: str
    profile: str
    preset: ComparisonPreset
    targets: tuple[str, ...]
    case_refs: tuple[str, ...]
    repetitions: int
    max_wall_seconds: float
    seed: int


@dataclass(frozen=True)
class SuiteEvaluationRequest:
    """Resolved request for one official or built-in benchmark Suite."""

    evaluation_id: str
    kind: Literal["suite"]
    bot: str
    suite: str
    case_ids: tuple[str, ...]
    preset: str
    repetitions: int
    max_wall_seconds: float
    seed: int
    options: dict[str, Any]
    confirm_external_write: bool
    dry_run: bool
    llm_judge: bool

    case_snapshot: dict[str, Any] | None = None


EvaluationRequest: TypeAlias = ComparisonEvaluationRequest | SuiteEvaluationRequest


@dataclass(frozen=True)
class TrialExecutionRequest:
    """One executor invocation inside a complete target group."""

    evaluation_id: str
    kind: EvaluationKind
    bot: str
    output: Path
    suite_id: str
    profile: str
    profile_case: ProfileCase | None
    case: EvalCase
    dimension: str
    target: EvaluationTarget
    attempt: int
    order: int
    plugin_id: str = ""
    driver_id: str = ""
    dry_run: bool = False
    llm_judge: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    confirm_external_write: bool = False
    max_execution_seconds: float = 0.0
    frozen_definition_snapshot: dict[str, Any] = field(default_factory=dict)
    frozen_definition_fingerprint: str = ""
    frozen_environment_fingerprint: str = ""


@dataclass(frozen=True)
class EvaluationTrial:
    """Coverage-complete evidence for one Case, attempt, and Target."""

    trial_id: str
    evaluation_id: str
    kind: EvaluationKind
    bot: str
    profile: str
    suite_id: str
    case_ref: str
    case_id: str
    dimension: str
    target_id: str
    target_fingerprint: str
    executor: TargetExecutor
    backend: str
    model: str
    reasoning_effort: str
    attempt: int
    order: int
    outcome: TrialOutcome
    expectation: CaseExpectation = field(default_factory=CaseExpectation)
    execution: ExecutionEvidence = field(default_factory=ExecutionEvidence)
    assessment: Assessment | None = None
    error: EvaluationError | None = None

    @property
    def score(self) -> float | None:
        return self.assessment.judge.score if self.assessment and self.assessment.judge else None

    @property
    def max_score(self) -> float:
        return self.assessment.judge.max_score if self.assessment and self.assessment.judge else 1.0

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"

    @property
    def judge(self) -> dict[str, Any] | None:
        return to_jsonable(self.assessment.judge) if self.assessment and self.assessment.judge else None

    @property
    def final_text(self) -> str:
        return self.execution.final_text

    @property
    def stop_reason(self) -> str:
        return self.execution.stop_reason

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return self.execution.events

    @property
    def usage_totals(self) -> dict[str, Any]:
        return self.execution.usage

    @property
    def tool_summary(self) -> dict[str, Any]:
        return self.execution.metadata.get("tool_summary", {})

    @property
    def evidence(self) -> dict[str, Any]:
        return {**self.execution.metadata,
                "judge_evidence": self.assessment.evidence if self.assessment else {},
                "error_stage": self.error.stage if self.error else "",
                "error_code": self.error.code if self.error else ""}

    @property
    def duration_seconds(self) -> float | None:
        return self.execution.total_seconds

    @property
    def started_at(self) -> str:
        return self.execution.started_at

    @property
    def finished_at(self) -> str:
        return self.execution.finished_at


@dataclass(frozen=True)
class CaseComparison:
    case_ref: str
    case_id: str
    dimension: str
    sample_size: int
    verdict: str
    targets: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class EvaluationResult:
    """Authoritative top-level Evaluation result."""

    evaluation_id: str
    kind: EvaluationKind
    bot: str
    status: EvaluationStatus
    started_at: str
    finished_at: str
    duration_seconds: float
    profile: str = ""
    suite: str = ""
    preset: str = ""
    repetitions: int = 1
    max_wall_seconds: float = 0.0
    seed: int = 0
    targets: tuple[EvaluationTarget, ...] = ()
    selected_cases: tuple[str, ...] = ()
    trials: tuple[EvaluationTrial, ...] = ()
    comparisons: tuple[CaseComparison, ...] = ()
    dimensions: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    schema_version: int = RESULT_SCHEMA_VERSION



Scorer = Callable[[], tuple[JudgeResult, dict[str, Any]]]


@dataclass(frozen=True)
class PreparedCase:
    observation: TrialObservation
    score: Scorer


CaseOpener = Callable[..., AbstractContextManager[PreparedCase]]
