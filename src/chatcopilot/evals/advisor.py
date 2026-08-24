"""Pure changed-path advisor for manually selected capability Evaluations.

This module never creates, starts, resumes, or mutates an Evaluation. It only
maps repository-relative changed paths to a validated selection suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from chatcopilot.evals.manifest import discover_suite_manifests, load_case_definitions

_AGENT_SUITE_ID = "agentstrata-capabilities-v1"
_QQ_FLOW_SUITE_ID = "agentstrata-qq-message-flow-v1"


@dataclass(frozen=True)
class EvaluationRunAdvice:
    """One independently runnable recommendation in the two-track center."""

    suite_id: str
    track: str
    case_ids: tuple[str, ...]
    recommended_preset: str


@dataclass(frozen=True)
class EvaluationAdvice:
    """Deterministic, side-effect-free recommendation for a manual run."""

    changed_paths: tuple[str, ...]
    categories: tuple[str, ...]
    reason: str
    runs: tuple[EvaluationRunAdvice, ...]
    external_checks: tuple[str, ...] = ()

    @property
    def case_ids(self) -> tuple[str, ...]:
        """Compatibility view for callers receiving exactly one recommended run."""

        return self.runs[0].case_ids if len(self.runs) == 1 else ()

    @property
    def recommended_preset(self) -> str | None:
        """Compatibility view for callers receiving exactly one recommended run."""

        return self.runs[0].recommended_preset if len(self.runs) == 1 else None


@dataclass(frozen=True)
class _PathRule:
    suite_id: str
    category: str
    prefixes: tuple[str, ...]
    exact_paths: tuple[str, ...]
    contains: tuple[str, ...]
    case_ids: tuple[str, ...]
    preset: str
    reason: str


@dataclass(frozen=True)
class _ExternalCheckRule:
    category: str
    prefixes: tuple[str, ...]
    exact_paths: tuple[str, ...]
    contains: tuple[str, ...]
    check_id: str
    reason: str


_RULES: tuple[_PathRule, ...] = (
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="search",
        prefixes=(
            "src/chatcopilot/agent/search/",
            "src/chatcopilot/agent/research/",
            "src/chatcopilot/agent/subagents/search_",
        ),
        exact_paths=("src/chatcopilot/search_probe.py",),
        contains=(),
        case_ids=(
            "search-general-with-evidence",
            "current-usd-cny-reference",
            "injection-untrusted-search-contained",
        ),
        preset="full",
        reason="搜索路由或证据变化需要当前 Bot 可运行的搜索与注入隔离覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="multimodal",
        prefixes=(),
        exact_paths=("src/chatcopilot/core/image_content.py",),
        contains=("/image_", "/images.py", "/attachment_", "/attachments/"),
        case_ids=(
            "image-ocr-order-number",
            "image-shape-spatial-count",
            "image-multi-input-order",
            "injection-untrusted-attachment-contained",
        ),
        preset="full",
        reason="图片或附件管线变化需要真实多模态 fixture、顺序和不可信附件隔离覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="access",
        prefixes=(),
        exact_paths=(
            "src/chatcopilot/core/access.py",
            "src/chatcopilot/middleware/access_control.py",
        ),
        contains=("/access/", "access_control", "allowlist", "whitelist"),
        case_ids=(
            "access-forbidden-tool-no-effect",
            "injection-untrusted-search-contained",
            "injection-untrusted-attachment-contained",
        ),
        preset="security",
        reason="角色、白名单或权限门禁变化需要完整 security 负例覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="code-task",
        prefixes=("src/chatcopilot/external_tools/dev/",),
        exact_paths=(
            "src/chatcopilot/code_task_service.py",
            "src/chatcopilot/contracts/code_tasks.py",
        ),
        contains=("code_task", "code-task", "background_coding_worker"),
        case_ids=(
            "code-fix-and-verify",
            "code-restart-and-health",
            "code-failure-no-false-success",
        ),
        preset="full",
        reason="代码任务、验证、交付或重启链路变化需要成功、健康和失败诚实性覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="tools",
        prefixes=(
            "src/chatcopilot/agent/tools/",
            "src/chatcopilot/external_tools/",
            "src/chatcopilot/tool_packs/",
        ),
        exact_paths=(
            "src/chatcopilot/contracts/tools.py",
            "src/chatcopilot/contracts/tool_packs.py",
            "src/chatcopilot/agent/context/prompt_plan.py",
        ),
        contains=("/tools/", "/tool_packs/"),
        case_ids=(
            "tool-allowed-exact-call",
            "tool-multistep-data-flow",
            "tool-disabled-hidden-no-effect",
            "tool-error-bounded-recovery",
            "access-forbidden-tool-no-effect",
        ),
        preset="full",
        reason="工具注册、选择、参数或执行变化需要正向、禁用和有界恢复覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="workspace",
        prefixes=(
            "src/chatcopilot/core/workspace_runtime/",
            "src/chatcopilot/middleware/runtime/workspace/",
            "src/chatcopilot/agent/tools/builtin/workspace/",
        ),
        exact_paths=(
            "src/chatcopilot/core/workspace.py",
            "src/chatcopilot/core/workspace_context.py",
            "src/chatcopilot/contracts/workspace.py",
            "src/chatcopilot/agent/tools/builtin/workspace_tools.py",
        ),
        contains=("/workspace/", "workspace_context", "workspace_tools"),
        case_ids=(
            "workspace-read-fixture",
            "workspace-write-contained",
            "injection-untrusted-attachment-contained",
        ),
        preset="full",
        reason="Workspace 解析、读写或交付变化需要 fixture、containment 和附件边界覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="runtime",
        prefixes=(
            "src/chatcopilot/core/",
            "src/chatcopilot/middleware/runtime/",
            "src/chatcopilot/application/",
        ),
        exact_paths=("src/chatcopilot/contracts/agent.py",),
        contains=("runtime_context", "runtime_env", "assembly"),
        case_ids=(
            "dialogue-strict-json",
            "tool-multistep-data-flow",
            "session-cross-user-isolation",
            "code-failure-no-false-success",
        ),
        preset="full",
        reason="共享 runtime 或 Agent 契约变化需要跨对话、工具、会话和错误语义覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="agent",
        prefixes=("src/chatcopilot/agent/",),
        exact_paths=(),
        contains=(),
        case_ids=(
            "dialogue-strict-json",
            "dialogue-clarify-before-action",
            "session-same-user-memory",
            "session-cross-user-isolation",
            "subagent-structured-result",
        ),
        preset="full",
        reason="Agent 行为或委托变化需要对话、记忆、隔离和结构化 subagent 覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="persona",
        prefixes=(
            "src/chatcopilot/core/persona_",
            "src/chatcopilot/contracts/persona_",
        ),
        exact_paths=(),
        contains=("persona_control", "persistent/persona"),
        case_ids=("persona-applied-behavior",),
        preset="quick",
        reason="人格上下文变化需要直接 Agent 人格行为覆盖。",
    ),
    _PathRule(
        suite_id=_AGENT_SUITE_ID,
        category="evals",
        prefixes=("src/chatcopilot/evals/", "tests/unit/test_eval", "tests/integration/test_eval"),
        exact_paths=(),
        contains=("/evaluation", "evaluation-"),
        case_ids=(),
        preset="quick",
        reason="Evaluation 自身变化建议先运行 quick，确认真实手动执行链路仍可用。",
    ),
)

_QQ_FLOW_RULES: tuple[_PathRule, ...] = (
    _PathRule(
        suite_id=_QQ_FLOW_SUITE_ID,
        category="qq-message-flow",
        prefixes=("src/chatcopilot/middleware/acp/",),
        exact_paths=(),
        contains=("/acp/",),
        case_ids=(),
        preset="full",
        reason="ACP 入站、会话或回复投影变化需要完整合成 QQ 后链路覆盖。",
    ),
    _PathRule(
        suite_id=_QQ_FLOW_SUITE_ID,
        category="qq-message-flow",
        prefixes=(),
        exact_paths=(
            "src/chatcopilot/core/access.py",
            "src/chatcopilot/middleware/access_control.py",
            "src/chatcopilot/middleware/acp/admission.py",
        ),
        contains=("/access/", "access_control", "admission", "allowlist", "whitelist"),
        case_ids=(
            "qq-group-missing-at-denied",
            "qq-attestation-mismatch-denied",
            "qq-member-owner-action-denied",
            "qq-nickname-spoof-denied",
        ),
        preset="security",
        reason="身份、白名单或权限变化需要 QQ 合成链路 security 失败关闭覆盖。",
    ),
    _PathRule(
        suite_id=_QQ_FLOW_SUITE_ID,
        category="qq-message-flow",
        prefixes=("src/chatcopilot/platforms/qq/",),
        exact_paths=("deploy/wsl/qq_gateway.sh",),
        contains=("/qq_gateway", "/onebot", "/napcat"),
        case_ids=(),
        preset="full",
        reason="QQ adapter 或 gateway 变化需要完整合成自有链路覆盖。",
    ),
    _PathRule(
        suite_id=_QQ_FLOW_SUITE_ID,
        category="qq-message-flow",
        prefixes=(
            "src/chatcopilot/core/persona_",
            "src/chatcopilot/contracts/persona_",
        ),
        exact_paths=(),
        contains=("persona_control", "persistent/persona"),
        case_ids=("qq-persona-persistence-next-turn",),
        preset="quick",
        reason="人格持久化变化需要 QQ Owner receipt 与下一轮 PromptPlan 覆盖。",
    ),
    _PathRule(
        suite_id=_QQ_FLOW_SUITE_ID,
        category="qq-message-flow",
        prefixes=("src/chatcopilot/evals/", "tests/unit/test_eval", "tests/integration/test_eval"),
        exact_paths=(),
        contains=("/evaluation", "evaluation-"),
        case_ids=(),
        preset="quick",
        reason="Evaluation 自身变化需要 QQ 后链路 quick 覆盖。",
    ),
)

_EXTERNAL_CHECK_RULES: tuple[_ExternalCheckRule, ...] = (
    _ExternalCheckRule(
        category="qq",
        prefixes=("src/chatcopilot/platforms/qq/",),
        exact_paths=("deploy/wsl/qq_gateway.sh",),
        contains=("/qq_gateway", "/onebot", "/napcat"),
        check_id="qq",
        reason=(
            "QQ/OneBot 链路变化建议手动运行 QQ 外部平台检查；"
            "它不属于 Agent Evaluation。"
        ),
    ),
)


def advise_capability_evaluation(changed_paths: Iterable[str]) -> EvaluationAdvice:
    """Return a validated recommendation without starting an Evaluation."""

    paths = _normalize_paths(changed_paths)
    contracts = {
        suite_id: _suite_contract(suite_id)
        for suite_id in (_AGENT_SUITE_ID, _QQ_FLOW_SUITE_ID)
    }
    all_rules = (*_RULES, *_QQ_FLOW_RULES)
    _validate_rules(all_rules, contracts)

    matched: list[_PathRule] = []
    matched_external: list[_ExternalCheckRule] = []
    unknown_paths: list[str] = []
    for path in paths:
        path_rules: list[_PathRule] = []
        for suite_id in (_AGENT_SUITE_ID, _QQ_FLOW_SUITE_ID):
            candidates = [
                candidate
                for candidate in all_rules
                if candidate.suite_id == suite_id and _matches(candidate, path)
            ]
            if candidates:
                path_rules.append(max(candidates, key=lambda item: _match_specificity(item, path)))
        external_rule = next(
            (candidate for candidate in _EXTERNAL_CHECK_RULES if _matches(candidate, path)),
            None,
        )
        if not path_rules and external_rule is None:
            unknown_paths.append(path)
        for path_rule in path_rules:
            if path_rule not in matched:
                matched.append(path_rule)
        if external_rule is not None and external_rule not in matched_external:
            matched_external.append(external_rule)

    selected = {suite_id: set() for suite_id in contracts}
    presets_requested = {suite_id: [] for suite_id in contracts}
    reasons: list[str] = []
    categories: list[str] = []
    for matched_rule in all_rules:
        if matched_rule not in matched:
            continue
        if matched_rule.category not in categories:
            categories.append(matched_rule.category)
        reasons.append(matched_rule.reason)
        presets_requested[matched_rule.suite_id].append(matched_rule.preset)
        contract = contracts[matched_rule.suite_id]
        selected[matched_rule.suite_id].update(
            matched_rule.case_ids or contract[1][matched_rule.preset]
        )
    external_checks: list[str] = []
    for external in _EXTERNAL_CHECK_RULES:
        if external not in matched_external:
            continue
        categories.append(external.category)
        reasons.append(external.reason)
        external_checks.append(external.check_id)
    if unknown_paths:
        categories.append("unknown")
        presets_requested[_AGENT_SUITE_ID].append("quick")
        selected[_AGENT_SUITE_ID].update(contracts[_AGENT_SUITE_ID][1]["quick"])
        reasons.append(f"{len(unknown_paths)} 个路径没有专用映射，保守退回 quick 代表性覆盖。")

    runs: list[EvaluationRunAdvice] = []
    for suite_id in (_AGENT_SUITE_ID, _QQ_FLOW_SUITE_ID):
        if not selected[suite_id]:
            continue
        ordered_case_ids, _presets, track = contracts[suite_id]
        requested = presets_requested[suite_id]
        recommended_preset = (
            requested[0] if len(set(requested)) == 1 else "custom"
        )
        runs.append(
            EvaluationRunAdvice(
                suite_id=suite_id,
                track=track,
                case_ids=tuple(
                    case_id for case_id in ordered_case_ids if case_id in selected[suite_id]
                ),
                recommended_preset=recommended_preset,
            )
        )
    return EvaluationAdvice(
        changed_paths=paths,
        categories=tuple(categories),
        reason=" ".join(reasons),
        runs=tuple(runs),
        external_checks=tuple(external_checks),
    )


def _suite_contract(
    suite_id: str,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], str]:
    manifests = [
        item for item in discover_suite_manifests() if item.suite_id == suite_id
    ]
    if len(manifests) != 1 or manifests[0].status != "implemented":
        raise RuntimeError(f"Evaluation Suite manifest is unavailable or ambiguous: {suite_id}")
    manifest = manifests[0]
    definitions = load_case_definitions(manifest)
    ordered_case_ids = tuple(item.case_id for item in definitions)
    presets = {item.preset_id: item.case_ids for item in manifest.presets}
    for required in ("quick", "full", "security"):
        if required not in presets:
            raise RuntimeError(f"Evaluation Suite is missing required preset: {required}")
    if manifest.track not in {"agent", "qq_message_flow"}:
        raise RuntimeError(f"Evaluation Suite has an invalid product track: {suite_id}")
    return ordered_case_ids, presets, manifest.track


def _validate_rules(
    rules: tuple[_PathRule, ...],
    contracts: dict[
        str,
        tuple[tuple[str, ...], dict[str, tuple[str, ...]], str],
    ],
) -> None:
    for rule in rules:
        try:
            ordered_case_ids, presets, _track = contracts[rule.suite_id]
        except KeyError as exc:
            raise RuntimeError(
                f"Advisor rule references unknown Evaluation Suite: {rule.suite_id}"
            ) from exc
        known_cases = set(ordered_case_ids)
        invalid = sorted(set(rule.case_ids) - known_cases)
        if invalid:
            raise RuntimeError(
                f"Advisor rule references unknown capability cases: {', '.join(invalid)}"
            )
        if rule.preset not in presets:
            raise RuntimeError(f"Advisor rule references unknown capability preset: {rule.preset}")
        outside_preset = sorted(set(rule.case_ids) - set(presets[rule.preset]))
        if outside_preset:
            raise RuntimeError(
                f"Advisor rule cases are not covered by preset {rule.preset}: "
                f"{', '.join(outside_preset)}"
            )


def _normalize_paths(changed_paths: Iterable[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in changed_paths:
        value = str(raw).strip()
        if not value or "\x00" in value or "\\" in value:
            raise ValueError("changed paths must be non-empty repository-relative POSIX paths")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"changed path escapes repository scope: {value}")
        while value.startswith("./"):
            value = value[2:]
        if not value or value == ".":
            raise ValueError("changed paths must name a repository entry")
        normalized.add(PurePosixPath(value).as_posix())
    if not normalized:
        raise ValueError("at least one changed path is required")
    return tuple(sorted(normalized))


def _matches(rule: _PathRule | _ExternalCheckRule, path: str) -> bool:
    return (
        path in rule.exact_paths
        or any(path.startswith(prefix) for prefix in rule.prefixes)
        or any(token in path for token in rule.contains)
    )


def _match_specificity(rule: _PathRule, path: str) -> tuple[int, int]:
    if path in rule.exact_paths:
        return (3, len(path))
    prefixes = [prefix for prefix in rule.prefixes if path.startswith(prefix)]
    if prefixes:
        return (2, max(len(prefix) for prefix in prefixes))
    tokens = [token for token in rule.contains if token in path]
    return (1, max((len(token) for token in tokens), default=0))


__all__ = ["EvaluationAdvice", "EvaluationRunAdvice", "advise_capability_evaluation"]
