"""Small orchestration contracts independent of evaluator and coding backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Iterator, Mapping
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Protocol


PIPELINE_VERSION = 9

ACTIVE = frozenset({"queued", "running", "cancel_requested"})
TERMINAL = frozenset({"fixed", "needs_review", "no_changes", "not_reproduced", "failed", "blocked", "cancelled", "interrupted"})


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class Cancelled(HarnessError):
    def __init__(self) -> None:
        super().__init__("cancelled", "修复任务已取消")


@dataclass(frozen=True)
class RepairFeedback:
    repair_hint: str = ""
    expected_behavior: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.repair_hint, str) or not isinstance(self.expected_behavior, str):
            raise ValueError("修复提示与参考答案必须为文本")

    def to_payload(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("repair_hint", self.repair_hint),
                ("expected_behavior", self.expected_behavior),
            )
            if value.strip()
        }


@dataclass(frozen=True)
class RepairOptions:
    model: str
    reasoning_effort: str = "medium"
    max_attempts: int = 3
    timeout_seconds: int = 3600
    single_issue: bool = False

    def __post_init__(self) -> None:
        if not self.model.strip() or any(char.isspace() for char in self.model):
            raise ValueError("修复模型必须明确指定")
        if self.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("不支持的推理强度")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("修复次数必须为正整数")
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise ValueError("任务时间预算必须为正整数")
        if type(self.single_issue) is not bool:
            raise ValueError("单问题模式必须为布尔值")


@dataclass(frozen=True)
class GovernanceSchedule:
    enabled: bool = False
    interval_hours: int = 24
    options: RepairOptions | None = None
    repair_hint: str = ""

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.interval_hours) is not int or self.interval_hours < 1:
            raise ValueError("定时开关必须为布尔值，间隔必须为正整数小时")
        if self.enabled and self.options is None:
            raise ValueError("启用定时代码熵回收必须明确模型与预算")
        if not isinstance(self.repair_hint, str):
            raise ValueError("熵回收提示必须为文本")

    @classmethod
    def from_payload(cls, value):
        return cls(value.get("enabled", False), value.get("interval_hours", 24),
                   RepairOptions(**value["options"]) if value.get("options") else None,
                   value.get("repair_hint", ""))

    def to_payload(self):
        return asdict(self)

@dataclass(frozen=True)
class CodingOptions:
    """Host-projected call options; None means no execution deadline."""
    model: str
    reasoning_effort: str
    max_attempts: int
    timeout_seconds: int | None


@dataclass(frozen=True)
class RepairRequest:
    source_kind: str
    options: RepairOptions
    request_id: str
    case_instance_id: str = ""
    bot_id: str = ""
    run_id: str = ""
    feedback: RepairFeedback = field(default_factory=RepairFeedback)

    def __post_init__(self) -> None:
        if self.source_kind != "code_health" and self.options.single_issue:
            raise ValueError("单问题模式仅适用于代码熵回收")
        if self.source_kind == "evaluation":
            if not self.case_instance_id or self.bot_id or self.run_id or self.feedback.expected_behavior.strip():
                raise ValueError("测评只接受 Case 实例和修复提示，不能提供人工答案")
        elif self.source_kind == "robot_task":
            if not self.bot_id or not self.run_id or self.case_instance_id:
                raise ValueError("机器人来源需要 bot_id 和 run_id")
        elif self.source_kind == "code_health":
            if self.bot_id or self.run_id or self.case_instance_id or self.feedback.expected_behavior.strip():
                raise ValueError("代码熵回收使用仓库契约，只接受可选回收提示")
        else:
            raise ValueError("未知修复来源")


@dataclass(frozen=True)
class ProblemEvidence:
    source_id: str
    kind: str
    digest: str
    material: dict[str, Any]
    feedback: RepairFeedback = field(default_factory=RepairFeedback)


@dataclass(frozen=True)
class RepairHypothesis:
    reason: str
    expected_behavior: str
    evidence_refs: tuple[str, ...] = ("source",)


@dataclass(frozen=True)
class CandidateRef:
    path: Path
    digest: str
    base_commit: str


@dataclass(frozen=True)
class VerificationPlan:
    primary_checks: tuple[str, ...]
    checks: tuple[str, ...]
    protected_checks: tuple[str, ...]
    repetitions: int
    real_agent: bool = False
    snapshot_id: str = ""
    check_repetitions: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, list[str]] = field(default_factory=dict)
    purpose: str = "repair"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> VerificationPlan:
        return cls(**{**value, **{key: tuple(value[key]) for key in
                                 ("primary_checks", "checks", "protected_checks")}})


@dataclass(frozen=True)
class VerificationCheck:
    check_id: str
    repetition: int
    outcome: str
    failure_kind: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationResult:
    run_id: str
    candidate_digest: str
    checks: tuple[VerificationCheck, ...]
    evidence_refs: tuple[str, ...] = ()

    def require_valid(self, check_ids: list[str], repetitions: int | dict[str, int]) -> None:
        expected = {(name, n) for name in check_ids for n in range(1, (repetitions[name] if isinstance(repetitions, dict) else repetitions) + 1)}
        actual = [(check.check_id, check.repetition) for check in self.checks]
        if len(actual) != len(expected) or set(actual) != expected:
            raise HarnessError("incomplete_verification", "验证结果缺失、重复或身份不一致")
        invalid = [check for check in self.checks if check.outcome not in {"passed", "failed"}
                   or (check.outcome == "failed" and check.failure_kind != "product")]
        if invalid:
            kinds = sorted({check.failure_kind or "evidence" for check in invalid})
            raise HarnessError("verification_" + kinds[0],
                               "验证未形成有效产品行为证据：" + ", ".join(kinds))

    @property
    def passed(self) -> set[str]:
        names = {check.check_id for check in self.checks}
        return {name for name in names if all(check.outcome == "passed"
                for check in self.checks if check.check_id == name)}


@dataclass(frozen=True)
class VerificationRequest(Mapping[str, Any]):
    """Only validation inputs cross the verifier port, never the full mutable task."""
    task_id: str
    source: dict[str, Any]
    acceptance: dict[str, Any]
    verification_plan: dict[str, Any] | None
    delivery: bool

    @classmethod
    def from_task(cls, task: dict[str, Any]) -> VerificationRequest:
        return cls(task["task_id"], task["source"], task.get("acceptance", {}),
                   task.get("verification_plan"), bool(task.get("delivery")))

    def __getitem__(self, key: str) -> Any:
        if key not in self.__dataclass_fields__:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


def acceptance_digest(task: dict[str, Any], attempt: dict[str, Any]) -> str:
    """Bind the receipt to the exact goal, test definition, environment and results."""
    source = task["source"]
    agent = source.get("agent_source", source)
    value = {"candidate": attempt["candidate_digest"], "goal": task.get("acceptance"),
        "plan": task.get("verification_plan"), "problem": task.get("problem_ref"),
        "principles": task.get("principles"), "environment": task.get("environment"),
        "test": source.get("test_sha256"), "case": agent.get("case_snapshot_id"),
        "conditions": agent.get("conditions"), "verification": attempt.get("verification"),
        "confirmation": attempt.get("confirmation"), "regressions": attempt.get("repository_regressions")}
    if source.get("kind") == "code_health":
        value["governance"] = {"target": source.get("governance_target"), "report": task.get("governance_report"),
                               "review": attempt.get("review")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SourceReader(Protocol):
    def load(self, reference: dict[str, str]) -> ProblemEvidence: ...


class Verifier(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def prepare(self, task: VerificationRequest, candidate: CandidateRef, output: Path,
                proposal: dict[str, Any], check_cancel: Callable[[], None]
                ) -> tuple[dict[str, Any], VerificationPlan]: ...
    def run(self, task: VerificationRequest, candidate: CandidateRef, run_id: str,
            checks: list[str], check_cancel: Callable[[], None]) -> VerificationResult: ...
    def regressions(self, task: VerificationRequest, candidate: CandidateRef,
                    check_cancel: Callable[[], None], checks: list[str] | None = None
                    ) -> dict[str, Any]: ...


class Publisher(Protocol):
    def publish(self, store: Any, task_id: str, attempt: dict[str, Any],
                check_cancel: Callable[[], None]) -> dict[str, Any]: ...


class Evaluator(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def source(self, evaluation_id: str, case_ref: str, target_id: str) -> dict[str, Any]: ...
    def run(
        self,
        task: dict[str, Any],
        worktree: Path,
        evaluation_id: str,
        case_ids: list[str],
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]: ...
    def cancel(self, evaluation_id: str) -> None: ...


class Coder(Protocol):
    def review(
        self,
        worktree: Path,
        evidence: dict[str, Any],
        options: RepairOptions,
        output: Path,
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]: ...

    def prepare(
        self,
        worktree: Path,
        evidence: dict[str, Any],
        options: RepairOptions,
        output: Path,
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]: ...

    def run(
        self,
        worktree: Path,
        evidence: dict[str, Any],
        options: RepairOptions,
        output: Path,
        check_cancel: Callable[[], None],
    ) -> dict[str, Any]: ...


def passed_cases(
    result: dict[str, Any], target_id: str, case_ids: list[str], repetitions: int
) -> set[str]:
    rows = [item for item in result.get("trials", []) if item.get("target_id") == target_id]
    expected = {(case, attempt) for case in case_ids for attempt in range(1, repetitions + 1)}
    identities = [(item.get("case_id"), item.get("attempt")) for item in rows]
    if len(identities) != len(expected) or set(identities) != expected:
        raise HarnessError("incomplete_trials", "测评 Trial 不完整或存在重复身份")
    return {
        case
        for case in case_ids
        if all(item["outcome"] == "passed" for item in rows if item["case_id"] == case)
    }


def review_decision(value: Any) -> dict[str, Any]:
    keys = {"decision", "problem", "reason", "evidence_refs"}
    if not isinstance(value, dict) or set(value) != keys:
        raise HarnessError("review_invalid", "审核结果不完整，未能确认修复")
    if value["decision"] not in {"approved", "rejected", "inconclusive"}:
        raise HarnessError("review_invalid", "审核结论无效，未能确认修复")
    if any(
        not isinstance(value[key], str) or len(value[key]) > 4000 for key in ("problem", "reason")
    ):
        raise HarnessError("review_invalid", "审核说明无效")
    if not value["reason"].strip() or (
        value["decision"] != "approved" and not value["problem"].strip()
    ):
        raise HarnessError("review_invalid", "审核缺少问题或理由")
    refs = value["evidence_refs"]
    if (
        not isinstance(refs, list)
        or not refs
        or any(
            not isinstance(ref, str)
            or ref not in {"source", "reproduction", "verification", "patch", "regression"}
            for ref in refs
        )
    ):
        raise HarnessError("review_invalid", "审核缺少有效证据引用")
    return value
