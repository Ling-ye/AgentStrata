"""Isolated Agent executor used by Profile comparison Evaluations."""

from __future__ import annotations

from chatcopilot.evals.execution_support import cleanup

from chatcopilot.application.execution_scope import execution_scope

import difflib
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from chatcopilot.application.agent_runtime import (
    AgentRuntimeAssemblyProfile,
    AgentRuntimeOverrides,
    assemble_agent_runtime,
)
from chatcopilot.contracts.agent import AgentTask
from chatcopilot.agent.context.prompt_plan import PromptBuildInput
from chatcopilot.contracts.agent_backend import CodexMainSessionPolicy
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.core.config import load_config
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.evals.adapters import gaia, ifeval
from chatcopilot.evals.artifact_ids import contained_artifact_path, trial_artifact_id
from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime, permission_filter
from chatcopilot.evals.execution_support import event_to_dict, usage_summary
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.profiles import ProfileCase
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService

_event_to_dict = event_to_dict
_usage_summary = usage_summary


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


@contextmanager
def open_isolated_case(request: IsolatedTrialRequest):
    """Execute one Profile Case in a policy-isolated Agent workspace."""

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
        allowed_tools = frozenset(str(value) for value in case.metadata.get("allowed_tools", []))
        evaluation_provider = _evaluation_tool_provider(case, tool_audit)
        subagents = _isolated_subagents(runtime.subagents)
        with _trial_environment(workspace, workspace_root):
            agent_runtime = assemble_agent_runtime(
                runtime,
                chat_config=chat_config,
                profile=AgentRuntimeAssemblyProfile.DETACHED,
                overrides=AgentRuntimeOverrides(
                    runtime_providers=(evaluation_provider,) if evaluation_provider else (),
                    rag_sources=(),
                    mcp_servers=(),
                    subagents=subagents,
                    agent_backend=request.target.backend,
                ),
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
                workspace_service=MiddlewareWorkspaceService(
                    workspace=workspace,
                    workspace_root=workspace_root,
                    execution_scope=execution_scope("owner", workspace.root, (workspace.root,)),
                ),
                permission_filter=permission_filter(allowed_tools),
                caller_role_hint="owner",
            )
            from chatcopilot.evals.trial_capture import record_turn, execution_phase
            from chatcopilot.evals.models import TrialObservation
            from chatcopilot.evals.models import PreparedCase
            task = _prepare_task(request.profile_case, workspace)
            record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": task.text, "completed": False})
            with execution_phase("agent"):
                result = session.run_task(task, on_event=lambda event: raw_events.append(event_to_dict(event)))
            record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": task.text,
                         "completed": True, "final_text": result.final_text, "stop_reason": result.stop_reason})
            observation = TrialObservation(final_text=result.final_text, stop_reason=result.stop_reason,
                events=tuple(raw_events), tool_calls=tuple(tool_audit), usage=usage_summary(raw_events).get("usage_totals", {}))
            yield PreparedCase(observation, lambda: judge_profile_trial(case, result.final_text, workspace_root, tool_audit, fixture_before))
    finally:
        if agent_runtime is not None:
            cleanup(agent_runtime.close)



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
            network_access=False,
            web_search_mode="disabled",
            sandbox_mode="read-only",
        ),
    )


def _evaluation_tool_provider(
    case: EvalCase,
    audit: list[dict[str, Any]],
) -> ToolProvider | None:
    if case.metadata.get("task_kind") != "tool":
        return None
    expected_key = str(case.metadata.get("expected_key", ""))
    expected_answer = str(case.metadata.get("expected_answer", ""))

    def lookup(
        args: Mapping[str, Any],
        _ctx: ToolContext,
    ) -> ToolResult:
        key = str(args.get("key", ""))
        ok = key == expected_key
        audit.append({"name": "lookup_eval_fact", "arguments": {"key": key}, "ok": ok})
        if not ok:
            return ToolResult(
                ok=False,
                error="invalid key",
                error_code="evaluation_key_invalid",
            )
        return ToolResult(
            ok=True,
            summary=expected_answer,
            data={"answer": expected_answer},
        )

    tool = ToolDef(
        name="lookup_eval_fact",
        summary="Return one deterministic evaluation fact by exact key.",
        input_schema=object_schema(
            {"key": {"type": "string", "description": "Exact evaluation key"}},
            required=("key",),
        ),
        output_schema=object_schema(
            {"answer": {"type": "string"}},
            required=("answer",),
        ),
        handler=lookup,
        category="eval.deterministic",
        owner="evals",
    )
    return ToolProvider(
        id="evals.isolated",
        packs={"runtime.session": (tool,)},
        module=__name__,
        description="Deterministic tools scoped to one isolated Evaluation trial.",
    )


def _prepare_task(item: ProfileCase, workspace: Workspace) -> AgentTask:
    if item.suite_id == "gaia-smoke":
        base = gaia.prepare_task(item.case, workspace)
    else:
        base = AgentTask(text=item.case.input, metadata={})
    allowed = (
        ", ".join(str(value) for value in item.case.metadata.get("allowed_tools", [])) or "none"
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
        return ifeval.judge(case, final_text), {"judge_kind": "deterministic:ifeval"}
    if adapter == "gaia" or case.case_id.startswith("gaia-"):
        return gaia.judge(case, final_text), {"judge_kind": "deterministic:gaia-exact"}
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
        1 for event in events if event.get("type") == "ToolFinished" and event.get("ok")
    )
    failed = sum(
        1 for event in events if event.get("type") == "ToolFinished" and not event.get("ok")
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
    "open_isolated_case",
    "judge_profile_trial",
    "load_evaluation_runtime",
    "permission_filter",
    "stage_fixture",
]
