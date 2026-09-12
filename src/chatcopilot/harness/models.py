"""Small orchestration contracts independent of evaluator and coding backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
import os

from chatcopilot.core.observability_redaction import redact_observability_payload

PIPELINE_VERSION = 2

ACTIVE = frozenset({"queued", "running", "cancel_requested"})
TERMINAL = frozenset({"fixed", "not_reproduced", "failed", "blocked", "cancelled", "interrupted"})


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class Cancelled(HarnessError):
    def __init__(self) -> None:
        super().__init__("cancelled", "修复任务已取消")


def safe_error(error: Exception, extra_secrets: tuple[str, ...] = ()) -> str:
    secrets = (
        *extra_secrets,
        *(
            value
            for name, value in os.environ.items()
            if any(
                part in name.lower() for part in ("secret", "token", "password", "api_key", "proxy")
            )
        ),
    )
    value = redact_observability_payload({"error": str(error)}, secrets=secrets).value
    return str(value.get("error", type(error).__name__))[:1000]


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
    timeout_seconds: int = 7200

    def __post_init__(self) -> None:
        if not self.model.strip() or any(char.isspace() for char in self.model):
            raise ValueError("修复模型必须明确指定")
        if self.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("不支持的推理强度")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("修复次数必须为正整数")
        if type(self.timeout_seconds) is not int or self.timeout_seconds < 1:
            raise ValueError("任务时间预算必须为正整数")


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

    def require_valid(self, check_ids: list[str], repetitions: int) -> None:
        expected = {(name, n) for name in check_ids for n in range(1, repetitions + 1)}
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


class SourceReader(Protocol):
    def load(self, reference: dict[str, str]) -> ProblemEvidence: ...


class Verifier(Protocol):
    def prepare(self, task: dict[str, Any], candidate: CandidateRef, coder: Coder,
                options: RepairOptions, check_cancel: Callable[[], None]
                ) -> tuple[dict[str, Any], RepairHypothesis, VerificationPlan]: ...
    def run(self, task: dict[str, Any], candidate: CandidateRef, run_id: str,
            checks: list[str], check_cancel: Callable[[], None]) -> VerificationResult: ...
    def regressions(self, task: dict[str, Any], candidate: CandidateRef,
                    check_cancel: Callable[[], None], checks: list[str] | None = None
                    ) -> dict[str, Any]: ...


class Publisher(Protocol):
    def publish(self, store: Any, task_id: str, attempt: dict[str, Any],
                check_cancel: Callable[[], None]) -> dict[str, Any]: ...


class Evaluator(Protocol):
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
