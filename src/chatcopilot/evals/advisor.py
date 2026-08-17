"""Pure changed-path advisor for manually selected capability Evaluations.

This module never creates, starts, resumes, or mutates an Evaluation. It only
maps repository-relative changed paths to a validated selection suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from chatcopilot.evals.manifest import discover_suite_manifests, load_case_definitions

_CAPABILITY_SUITE_ID = "agentstrata-capabilities-v1"


@dataclass(frozen=True)
class EvaluationAdvice:
    """Deterministic, side-effect-free recommendation for a manual run."""

    changed_paths: tuple[str, ...]
    categories: tuple[str, ...]
    reason: str
    case_ids: tuple[str, ...]
    recommended_preset: str


@dataclass(frozen=True)
class _PathRule:
    category: str
    prefixes: tuple[str, ...]
    exact_paths: tuple[str, ...]
    contains: tuple[str, ...]
    case_ids: tuple[str, ...]
    preset: str
    reason: str


_RULES: tuple[_PathRule, ...] = (
    _PathRule(
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
            "search-explicit-source",
            "search-conflict-disclosure",
            "injection-untrusted-search-contained",
        ),
        preset="full",
        reason="搜索路由、来源约束或证据合并变化需要完整搜索与注入隔离覆盖。",
    ),
    _PathRule(
        category="multimodal",
        prefixes=(),
        exact_paths=("src/chatcopilot/core/image_content.py",),
        contains=("/image_", "/images.py", "/attachment_", "/attachments/"),
        case_ids=(
            "attachment-remote-reference-not-local",
            "image-ocr-order-number",
            "image-shape-spatial-count",
            "image-multi-input-order",
            "injection-untrusted-attachment-contained",
        ),
        preset="full",
        reason="图片或附件管线变化需要真实多模态 fixture、顺序和不可信附件隔离覆盖。",
    ),
    _PathRule(
        category="access",
        prefixes=(),
        exact_paths=(
            "src/chatcopilot/core/access.py",
            "src/chatcopilot/middleware/access_control.py",
            "src/chatcopilot/middleware/acp/access_gate.py",
        ),
        contains=("/access/", "access_control", "access_gate", "allowlist", "whitelist"),
        case_ids=(
            "access-member-owner-tool-denied",
            "access-nickname-spoof-denied",
            "access-forbidden-tool-no-effect",
            "injection-untrusted-search-contained",
            "injection-untrusted-attachment-contained",
        ),
        preset="security",
        reason="角色、白名单或权限门禁变化需要完整 security 负例覆盖。",
    ),
    _PathRule(
        category="qq",
        prefixes=("src/chatcopilot/platforms/qq/",),
        exact_paths=(
            "deploy/wsl/qq_gateway.sh",
            "src/chatcopilot/evals/qq_live_driver.py",
        ),
        contains=("/qq_gateway", "/onebot", "/napcat"),
        case_ids=(
            "qq-private-text-roundtrip",
            "qq-group-mention-roundtrip",
            "qq-group-image-roundtrip",
        ),
        preset="qq-live",
        reason="QQ/OneBot 链路变化建议人工确认后运行受限真实 QQ 正向链路。",
    ),
    _PathRule(
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
        category="tools",
        prefixes=(
            "src/chatcopilot/agent/tools/",
            "src/chatcopilot/external_tools/",
            "src/chatcopilot/tool_packs/",
        ),
        exact_paths=(
            "src/chatcopilot/contracts/tools.py",
            "src/chatcopilot/contracts/tool_packs.py",
            "src/chatcopilot/botspec/tool_pack_prompt.py",
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
            "attachment-remote-reference-not-local",
            "injection-untrusted-attachment-contained",
        ),
        preset="full",
        reason="Workspace 解析、读写或交付变化需要 fixture、containment 和附件边界覆盖。",
    ),
    _PathRule(
        category="acp",
        prefixes=("src/chatcopilot/middleware/acp/",),
        exact_paths=(),
        contains=("/acp/",),
        case_ids=(
            "dialogue-strict-json",
            "attachment-remote-reference-not-local",
            "session-same-user-memory",
            "session-cross-user-isolation",
            "access-member-owner-tool-denied",
            "access-nickname-spoof-denied",
        ),
        preset="full",
        reason="ACP turn、session 或 adapter 边界变化需要会话、附件和身份隔离覆盖。",
    ),
    _PathRule(
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
        category="evals",
        prefixes=("src/chatcopilot/evals/", "tests/unit/test_eval", "tests/integration/test_eval"),
        exact_paths=(),
        contains=("/evaluation", "evaluation-"),
        case_ids=(),
        preset="quick",
        reason="Evaluation 自身变化建议先运行 quick，确认真实手动执行链路仍可用。",
    ),
)


def advise_capability_evaluation(changed_paths: Iterable[str]) -> EvaluationAdvice:
    """Return a validated recommendation without starting an Evaluation."""

    paths = _normalize_paths(changed_paths)
    ordered_case_ids, presets = _capability_contract()
    _validate_rules(ordered_case_ids, presets)

    matched: list[_PathRule] = []
    unknown_paths: list[str] = []
    for path in paths:
        rule = next((candidate for candidate in _RULES if _matches(candidate, path)), None)
        if rule is None:
            unknown_paths.append(path)
        elif rule not in matched:
            matched.append(rule)

    selected: set[str] = set()
    reasons: list[str] = []
    presets_requested: list[str] = []
    categories: list[str] = []
    for rule in _RULES:
        if rule not in matched:
            continue
        categories.append(rule.category)
        reasons.append(rule.reason)
        presets_requested.append(rule.preset)
        selected.update(rule.case_ids or presets["quick"])
    if unknown_paths:
        categories.append("unknown")
        presets_requested.append("quick")
        selected.update(presets["quick"])
        reasons.append(f"{len(unknown_paths)} 个路径没有专用映射，保守退回 quick 代表性覆盖。")

    known_cases = set(ordered_case_ids)
    invalid = sorted(selected - known_cases)
    if invalid:
        raise RuntimeError(
            f"Advisor rule references unknown capability cases: {', '.join(invalid)}"
        )
    for preset in presets_requested:
        if preset not in presets:
            raise RuntimeError(f"Advisor rule references unknown capability preset: {preset}")

    case_ids = tuple(case_id for case_id in ordered_case_ids if case_id in selected)
    recommended_preset = (
        presets_requested[0] if presets_requested and len(set(presets_requested)) == 1 else "custom"
    )
    return EvaluationAdvice(
        changed_paths=paths,
        categories=tuple(categories),
        reason=" ".join(reasons),
        case_ids=case_ids,
        recommended_preset=recommended_preset,
    )


def _capability_contract() -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    manifests = [
        item for item in discover_suite_manifests() if item.suite_id == _CAPABILITY_SUITE_ID
    ]
    if len(manifests) != 1 or manifests[0].status != "implemented":
        raise RuntimeError("capability Suite manifest is unavailable or ambiguous")
    manifest = manifests[0]
    definitions = load_case_definitions(manifest)
    ordered_case_ids = tuple(item.case_id for item in definitions)
    presets = {item.preset_id: item.case_ids for item in manifest.presets}
    for required in ("quick", "full", "security", "qq-live"):
        if required not in presets:
            raise RuntimeError(f"capability Suite is missing required preset: {required}")
    return ordered_case_ids, presets


def _validate_rules(
    ordered_case_ids: tuple[str, ...],
    presets: dict[str, tuple[str, ...]],
) -> None:
    known_cases = set(ordered_case_ids)
    for rule in _RULES:
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


def _matches(rule: _PathRule, path: str) -> bool:
    return (
        path in rule.exact_paths
        or any(path.startswith(prefix) for prefix in rule.prefixes)
        or any(token in path for token in rule.contains)
    )


__all__ = ["EvaluationAdvice", "advise_capability_evaluation"]
