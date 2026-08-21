"""Isolated Agent executor used by Profile comparison Evaluations."""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from chatcopilot.agent.protocol import AgentTask
from chatcopilot.agent.runtime import build_agent_runtime
from chatcopilot.agent.context.prompt_plan import PromptBuildInput
from chatcopilot.botspec import assemble_runtime_context, load_botspec, resolve_bot_spec_path
from chatcopilot.botspec.runtime_env import load_research_llm_config
from chatcopilot.contracts.agent_backend import CodexMainSessionPolicy
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.core.config import load_config
from chatcopilot.core.workspace import Workspace
from chatcopilot.evals.adapters import gaia, ifeval
from chatcopilot.evals.artifact_ids import contained_artifact_path, trial_artifact_id
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.profiles import ProfileCase
from chatcopilot.evals.redaction import collect_env_secrets, redact_payload, sanitize_text
from chatcopilot.evals.runner import _event_to_dict, _load_local_env, _usage_summary
from chatcopilot.middleware.runtime.workspace import MiddlewareWorkspaceService


@dataclass(frozen=True)
class IsolatedTarget:
    target_id: str
    backend: str
    label: str
    fingerprint: str
    model: str = ""
    reasoning_effort: str = ""


@dataclass(frozen=True)
class IsolatedTrialRequest:
    bot: str
    evaluation_id: str
    output: Path
    profile_case: ProfileCase
    target: IsolatedTarget
    attempt: int
    order: int


@dataclass(frozen=True)
class IsolatedTrialResult:
    trial_id: str
    case_ref: str
    suite_id: str
    case_id: str
    dimension: str
    target_id: str
    backend: str
    attempt: int
    outcome: str
    score: float = 0.0
    passed: bool = False
    duration_seconds: float = 0.0
    final_text: str = ""
    stop_reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    judge: dict[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()
    usage_totals: dict[str, int] | None = None
    tool_summary: dict[str, int] | None = None
    evidence: dict[str, Any] | None = None
    error: str = ""


def execute_isolated_trial(request: IsolatedTrialRequest) -> IsolatedTrialResult:
    """Execute one Profile Case in a policy-isolated Agent workspace."""

    started = time.monotonic()
    started_at = _utc_now()
    case = request.profile_case.case
    trial_id = trial_artifact_id(
        request.profile_case.case_id,
        attempt=request.attempt,
        target_fingerprint=request.target.fingerprint,
    )
    workspace_root = contained_artifact_path(
        request.output,
        "workspaces",
        trial_id,
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    fixture_before = _stage_fixture(case, workspace_root)
    tool_audit: list[dict[str, Any]] = []
    raw_events: list[dict[str, Any]] = []
    agent_runtime = None
    final_text = ""
    stop_reason = ""
    judge: JudgeResult | None = None
    verification: dict[str, Any] = {}
    error = ""
    outcome = "error"
    try:
        runtime = load_evaluation_runtime(request.bot)
        chat_config = load_config(env_prefix=runtime.spec.llm.env_prefix)
        workspace = Workspace(
            root=workspace_root.resolve(),
            chat_kind="p2p",
            chat_id=f"eval:{request.evaluation_id}:{trial_id}",
            user_id="eval-user",
            user_name="Eval Runner",
        ).ensure()
        allowed_tools = frozenset(
            str(value) for value in case.metadata.get("allowed_tools", [])
        )
        extra_tools = _extra_tools(case, tool_audit)
        subagents = _isolated_subagents(runtime.subagents)
        with _trial_environment(workspace, workspace_root):
            agent_runtime = build_agent_runtime(
                chat_config=chat_config,
                research_llm_config=load_research_llm_config(
                    runtime.spec.llm,
                    fallback=chat_config.llm,
                ),
                tool_packs=runtime.tool_packs,
                exclude_tools=runtime.exclude_tools,
                extra_tools=extra_tools,
                skill_index=runtime.skills,
                rag_sources=(),
                mcp_servers=(),
                subagents=subagents,
                agent_backend=request.target.backend,
            )
            session = agent_runtime.new_session(
                session_id=f"eval-{request.evaluation_id}-{trial_id}",
                prompt_input=PromptBuildInput(
                    profile=runtime.prompt_profile,
                    backend=request.target.backend,
                    model=None,
                    role="owner",
                    channel_kind="private",
                    session_policy="这是隔离 Evaluation Trial；只处理当前冻结 Case。",
                    capability_policies=runtime.capability_policies,
                    skill_index=runtime.skills,
                ),
                workspace_service=MiddlewareWorkspaceService(),
                permission_filter=permission_filter(allowed_tools),
                caller_role_hint="owner",
            )
            result = session.run_task(
                _prepare_task(request.profile_case, workspace),
                on_event=lambda event: raw_events.append(_event_to_dict(event)),
            )
            final_text = result.final_text
            stop_reason = result.stop_reason
            judge, verification = judge_profile_trial(
                case,
                final_text,
                workspace_root,
                tool_audit,
                fixture_before,
            )
            outcome = "passed" if judge.passed else "failed"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if agent_runtime is not None:
            agent_runtime.close()

    roots = {"evaluation": request.output, "workspace": workspace_root}
    secrets = collect_env_secrets()
    sanitized_events = redact_payload(raw_events, secrets=secrets, roots=roots)
    usage = _usage_summary(sanitized_events).get("usage_totals", {})
    judge_payload = asdict(judge) if judge is not None else None
    return IsolatedTrialResult(
        trial_id=trial_id,
        case_ref=request.profile_case.ref,
        suite_id=request.profile_case.suite_id,
        case_id=request.profile_case.case_id,
        dimension=request.profile_case.dimension,
        target_id=request.target.target_id,
        backend=request.target.backend,
        attempt=request.attempt,
        outcome=outcome,
        score=(
            judge.score / judge.max_score
            if judge is not None and judge.max_score
            else 0.0
        ),
        passed=bool(judge and judge.passed),
        duration_seconds=time.monotonic() - started,
        final_text=sanitize_text(final_text, secrets=secrets, roots=roots),
        stop_reason=stop_reason,
        started_at=started_at,
        finished_at=_utc_now(),
        judge=redact_payload(judge_payload, secrets=secrets, roots=roots),
        events=tuple(sanitized_events),
        usage_totals={
            str(key): int(value)
            for key, value in usage.items()
            if isinstance(value, int)
        },
        tool_summary=_summarize_tools(raw_events, tool_audit),
        evidence=redact_payload(verification, secrets=secrets, roots=roots),
        error=sanitize_text(error, secrets=secrets, roots=roots),
    )


def load_evaluation_runtime(bot: str) -> Any:
    """Load the Bot runtime and its private environment for execution."""

    candidate: str | Path = (
        Path(bot) if any(char in bot for char in ("/", "\\")) else bot
    )
    runtime = assemble_runtime_context(load_botspec(resolve_bot_spec_path(candidate)))
    _load_local_env(runtime.source_path.parent / "local.env")
    return runtime


def permission_filter(allowed: frozenset[str]) -> Callable[[ToolDef], str | None]:
    """Deny every tool not explicitly listed by the Profile Case."""

    def check(tool: ToolDef) -> str | None:
        if tool.name in allowed:
            return None
        return "evaluation policy denies this tool"

    return check


def stage_fixture(case: EvalCase, root: Path) -> dict[str, str]:
    """Stage a deterministic code fixture inside the isolated workspace."""

    return _stage_fixture(case, root)


def judge_profile_trial(
    case: EvalCase,
    final_text: str,
    workspace_root: Path,
    tool_audit: Sequence[dict[str, Any]],
    fixture_before: Mapping[str, str],
) -> tuple[JudgeResult, dict[str, Any]]:
    """Apply the Profile Case's deterministic judge."""

    return _judge_trial(
        case,
        final_text,
        workspace_root,
        tool_audit,
        fixture_before,
    )


def _isolated_subagents(value: SubagentSpec) -> SubagentSpec:
    return replace(
        value,
        include=(),
        custom=(),
        overrides={},
        workflows=(),
        research_enabled=False,
        codex=CodexMainSessionPolicy(
            owner_access="workspace",
            member_access="workspace",
            network_access=False,
            web_search_mode="disabled",
            sandbox_mode="read-only",
        ),
    )


def _extra_tools(case: EvalCase, audit: list[dict[str, Any]]) -> tuple[ToolDef, ...]:
    if case.metadata.get("task_kind") != "tool":
        return ()
    expected_key = str(case.metadata.get("expected_key", ""))
    expected_answer = str(case.metadata.get("expected_answer", ""))

    def lookup(
        args: Mapping[str, Any],
        _ctx: Any = None,
    ) -> tuple[str, list[str], str | None]:
        key = str(args.get("key", ""))
        ok = key == expected_key
        audit.append(
            {"name": "lookup_eval_fact", "arguments": {"key": key}, "ok": ok}
        )
        if not ok:
            return "unknown evaluation key", [], "invalid key"
        return expected_answer, [], None

    return (
        ToolDef(
            name="lookup_eval_fact",
            summary="Return one deterministic evaluation fact by exact key.",
            properties={
                "key": {"type": "string", "description": "Exact evaluation key"}
            },
            required=["key"],
            handler=lookup,
            category="eval.deterministic",
            owner="evals",
        ),
    )


def _prepare_task(item: ProfileCase, workspace: Workspace) -> AgentTask:
    if item.suite_id == "gaia-smoke":
        base = gaia.prepare_task(item.case, workspace)
    else:
        base = AgentTask(text=item.case.input, metadata={})
    allowed = (
        ", ".join(str(value) for value in item.case.metadata.get("allowed_tools", []))
        or "none"
    )
    context = "\n".join(
        (
            "## Evaluation isolation policy",
            f"case_id: {item.case_id}",
            f"dimension: {item.dimension}",
            f"allowed_tools: {allowed}",
            "Do not send messages, deploy, persist memory, write Wiki data, commit, or push.",
            "Only the isolated evaluation workspace may be modified.",
            item.case.expected_behavior,
        )
    )
    if base.turn_context:
        context = f"{base.turn_context}\n\n{context}"
    return AgentTask(
        text=base.text,
        resources=base.resources,
        turn_context=context,
        metadata={
            "eval_profile": "agent-comparison-mvp",
            "eval_suite": item.suite_id,
            "eval_case": item.case_id,
        },
    )


def _stage_fixture(case: EvalCase, root: Path) -> dict[str, str]:
    files = case.metadata.get("fixture_files")
    if not isinstance(files, dict):
        return {}
    snapshot: dict[str, str] = {}
    for relative, content in files.items():
        path = (root / str(relative)).resolve()
        if root.resolve() not in path.parents:
            raise ValueError(f"fixture path escapes workspace: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(content)
        path.write_text(text, encoding="utf-8")
        snapshot[str(relative)] = text
    return snapshot


def _judge_trial(
    case: EvalCase,
    final_text: str,
    workspace_root: Path,
    tool_audit: Sequence[dict[str, Any]],
    fixture_before: Mapping[str, str],
) -> tuple[JudgeResult, dict[str, Any]]:
    adapter = str(case.metadata.get("adapter", ""))
    if adapter == "ifeval":
        return ifeval.judge(case, final_text), {
            "judge_kind": "deterministic:ifeval"
        }
    if adapter == "gaia" or case.case_id.startswith("gaia-"):
        return gaia.judge(case, final_text), {
            "judge_kind": "deterministic:gaia-exact"
        }
    if case.metadata.get("task_kind") == "tool":
        expected_tool = str(case.metadata.get("expected_tool", ""))
        expected_key = str(case.metadata.get("expected_key", ""))
        expected_answer = str(case.metadata.get("expected_answer", ""))
        call_ok = any(
            item.get("name") == expected_tool
            and item.get("arguments", {}).get("key") == expected_key
            and item.get("ok") is True
            for item in tool_audit
        )
        answer_ok = final_text.strip() == expected_answer
        score = (int(call_ok) + int(answer_ok)) / 2
        reasons = tuple(
            reason
            for ok, reason in (
                (call_ok, "exact tool call"),
                (answer_ok, "exact returned value"),
            )
            if ok
        )
        missing = tuple(
            reason
            for ok, reason in (
                (call_ok, "exact tool call"),
                (answer_ok, "exact returned value"),
            )
            if not ok
        )
        return (
            JudgeResult(
                score=score,
                max_score=1.0,
                passed=call_ok and answer_ok,
                reasons=reasons,
                missing=missing,
            ),
            {
                "judge_kind": "deterministic:tool-audit",
                "tool_audit": list(tool_audit),
            },
        )
    if case.metadata.get("task_kind") == "code":
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "-q"],
            cwd=workspace_root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        after = {
            relative: (workspace_root / relative).read_text(encoding="utf-8")
            for relative in fixture_before
            if (workspace_root / relative).is_file()
        }
        diffs: list[str] = []
        for relative, before_text in fixture_before.items():
            after_text = after.get(relative, "")
            if before_text == after_text:
                continue
            diffs.extend(
                difflib.unified_diff(
                    before_text.splitlines(),
                    after_text.splitlines(),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                    lineterm="",
                )
            )
        passed = completed.returncode == 0
        judge = JudgeResult(
            score=1.0 if passed else 0.0,
            max_score=1.0,
            passed=passed,
            reasons=(
                ("fixed verification command passed",)
                if passed
                else ("fixed verification command failed",)
            ),
        )
        return judge, {
            "judge_kind": "deterministic:verification-command",
            "command": [sys.executable, "-m", "unittest", "-q"],
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "diff": "\n".join(diffs),
        }
    raise ValueError(f"no deterministic judge for case {case.case_id}")


def _summarize_tools(
    events: Sequence[dict[str, Any]],
    audit: Sequence[dict[str, Any]],
) -> dict[str, int]:
    successful = sum(
        1
        for event in events
        if event.get("type") == "ToolFinished" and event.get("ok")
    )
    failed = sum(
        1
        for event in events
        if event.get("type") == "ToolFinished" and not event.get("ok")
    )
    successful += sum(1 for item in audit if item.get("ok"))
    failed += sum(1 for item in audit if not item.get("ok"))
    return {"successful": successful, "failed": failed}


@contextmanager
def _trial_environment(workspace: Workspace, dev_root: Path) -> Iterator[None]:
    values = {
        "CHATCOPILOT_WORKSPACE": str(workspace.root),
        "CHATCOPILOT_CHAT_KIND": workspace.chat_kind or "",
        "CHATCOPILOT_CHAT_ID": workspace.chat_id or "",
        "CHATCOPILOT_USER_ID": workspace.user_id or "",
        "CHATCOPILOT_USER_NAME": workspace.user_name or "",
        "CHATCOPILOT_DEV_ROOT": str(dev_root),
    }
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "IsolatedTarget",
    "IsolatedTrialRequest",
    "IsolatedTrialResult",
    "execute_isolated_trial",
    "judge_profile_trial",
    "load_evaluation_runtime",
    "permission_filter",
    "stage_fixture",
]
