"""Existing driver policies exposed as single-Case observation/scorer contexts."""

from __future__ import annotations

from chatcopilot.evals.execution_support import cleanup

from contextlib import contextmanager
from pathlib import Path
import os
from typing import Any
from chatcopilot.core.config import ChatConfig, load_config
from chatcopilot.contracts.agent import AgentTask
from chatcopilot.botspec import assemble_runtime_context, load_botspec, resolve_bot_spec_path
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.evals.execution_support import load_local_env as _load_local_env
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.plugins import EvaluationPlugin
from chatcopilot.project import ENV_PREFIX

from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.result_codec import ResultContractError
from chatcopilot.evals.trial_capture import execution_phase, record_turn
from chatcopilot.evals.models import PreparedCase


@contextmanager
def open_case(case, *, suite_id, bot, workspace_root, options, driver,
              confirm_external_write, profile_request=None):
    from chatcopilot.evals.plugins import get_evaluation_plugin
    from chatcopilot.evals.registry import get_manifest
    if profile_request is not None:
        from chatcopilot.evals.isolated_executor import open_isolated_case
        with open_isolated_case(profile_request) as prepared:
            yield prepared
        return
    manifest = get_manifest(suite_id)
    plugin = get_evaluation_plugin(manifest.plugin_id)
    driver = driver or str(manifest.driver_id)
    if driver == "direct_llm":
        with _direct_model(case, plugin=plugin, suite_id=suite_id, bot=bot, options=options) as prepared:
            yield prepared
    elif plugin.open_case is not None:
        with plugin.open_case(case, bot=bot, workspace_root=workspace_root, options=options) as prepared:
            yield prepared
    elif isinstance(case.metadata.get("case_definition"), dict):
        from chatcopilot.evals.capability_executor import open_capability_case
        with open_capability_case(case, suite_id=suite_id, bot=bot, workspace_root=workspace_root,
                                  options=options, confirm_external_write=confirm_external_write) as prepared:
            yield prepared
    else:
        with _agent(case, plugin=plugin, suite_id=suite_id, bot=bot,
                    workspace_root=workspace_root, options=options) as prepared:
            yield prepared


@contextmanager
def _direct_model(case, *, plugin, suite_id, bot, options):
    from chatcopilot.evals.benchmark_scoring import score_benchmark
    if plugin.execute_model is None or plugin.judge is None:
        raise ResultContractError("direct_llm: execute_model and judge hooks are required")
    config = _load_bot_config(bot)
    record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": case.input, "completed": False})
    with execution_phase("agent"):
        raw = plugin.execute_model(case, chat_config=config)
    if not isinstance(raw, dict):
        raise ResultContractError("model observation: expected object")
    metadata, calls, usage = raw.get("metadata", {}), raw.get("tool_calls", []), raw.get("usage", {})
    if not isinstance(metadata, dict) or not isinstance(calls, list) or not isinstance(usage, dict):
        raise ResultContractError("model observation: malformed metadata, calls or usage")
    final_text = raw.get("final_text") or ""
    record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": case.input,
                 "completed": True, "final_text": final_text, "stop_reason": "end_turn",
                 **{key: metadata[key] for key in ("model_request", "model_response") if key in metadata}})
    observation = TrialObservation(final_text=final_text, stop_reason="end_turn", tool_calls=tuple(calls),
        usage=_flatten_usage(usage), evidence=({"kind": "adapter_metadata", "input": case.input, **metadata},))
    yield PreparedCase(observation, lambda: score_benchmark(
        suite_id, case, final_text, lambda: plugin.judge(case, raw), options=options, tool_calls=calls))


@contextmanager
def _agent(case, *, plugin, suite_id, bot, workspace_root, options):
    from chatcopilot.application.agent_runtime import AgentRuntimeAssemblyProfile, assemble_agent_runtime
    from chatcopilot.agent.context.prompt_plan import PromptBuildInput
    from chatcopilot.core.config import load_config
    from chatcopilot.core.workspace_runtime import Workspace, MiddlewareWorkspaceService
    from chatcopilot.evals.evaluation_runtime import load_evaluation_runtime
    from chatcopilot.evals.execution_support import event_to_dict, usage_summary
    from chatcopilot.evals.benchmark_scoring import score_benchmark

    runtime = load_evaluation_runtime(bot)
    agent = assemble_agent_runtime(runtime, chat_config=load_config(env_prefix=runtime.spec.llm.env_prefix),
                                   profile=AgentRuntimeAssemblyProfile.DETACHED)
    try:
        workspace = Workspace(root=Path(workspace_root).resolve(), chat_kind="p2p", chat_id=f"eval:{suite_id}",
                              user_id="eval-user", user_name="Eval Runner").ensure()
        with _EvalWorkspaceEnv(workspace):
            session = agent.new_session(session_id=f"eval-{suite_id}-{case.case_id}",
                prompt_input=PromptBuildInput(profile=runtime.prompt_profile, backend=runtime.agent_backend,
                    model=None, role="owner", channel_kind="private",
                    session_policy="这是隔离 Evaluation 会话；只处理当前评测 Case。",
                    capability_policies=runtime.capability_policies, skill_index=runtime.skills),
                workspace_service=MiddlewareWorkspaceService())
            task = _prepare_task(plugin, suite_id, case, workspace)
            events = []
            record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": task.text, "completed": False})
            with execution_phase("agent"):
                result = session.run_task(task, on_event=lambda event: events.append(event_to_dict(event)))
            record_turn({"conversation_id": case.case_id, "turn_index": 0, "input": task.text,
                         "completed": True, "final_text": result.final_text, "stop_reason": result.stop_reason})
            observation = TrialObservation(final_text=result.final_text, stop_reason=result.stop_reason,
                events=tuple(events), usage=usage_summary(events).get("usage_totals", {}))
            yield PreparedCase(observation, lambda: score_benchmark(suite_id, case, result.final_text,
                lambda: _judge_case(plugin, case, result.final_text), options=options))
    finally:
        cleanup(agent.close)


def _flatten_usage(usage: Any) -> dict[str, int]:
    if isinstance(usage, dict):
        totals: dict[str, int] = {}
        for key, value in usage.items():
            if isinstance(value, int):
                totals[key] = value
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, int):
                        totals[nested_key] = totals.get(nested_key, 0) + nested_value
                        totals[f"{key}.{nested_key}"] = nested_value
        return totals
    if hasattr(usage, "model_dump"):
        return _flatten_usage(usage.model_dump())
    return {}


def _judge_case(
    plugin: EvaluationPlugin,
    case: EvalCase,
    final_text: str,
    *,
    chat_config: ChatConfig | None = None,
) -> JudgeResult:
    if plugin.judge is None:
        raise ValueError(
            f"agent plugin {plugin.plugin_id!r} must define a deterministic judge hook"
        )
    result = plugin.judge(case, final_text, chat_config=chat_config)
    if not isinstance(result, JudgeResult):
        raise TypeError(f"evaluation plugin {plugin.plugin_id!r} returned an invalid judge result")
    return result


def _load_bot_config(bot: str) -> ChatConfig:
    """Load ChatConfig from a BotSpec path (for BFCL and other LLM-only paths)."""

    runtime = assemble_runtime_context(
        load_botspec(resolve_bot_spec_path(Path(bot) if _looks_like_path(bot) else bot))
    )
    _load_local_env(runtime.source_path.parent / "local.env")
    return load_config(env_prefix=runtime.spec.llm.env_prefix)


def _prepare_task(
    plugin: EvaluationPlugin,
    suite_id: str,
    case: EvalCase,
    workspace: Workspace,
) -> AgentTask:
    if plugin.build_task is not None:
        task = plugin.build_task(case, workspace)
        if not isinstance(task, AgentTask):
            raise TypeError(f"evaluation plugin {plugin.plugin_id!r} returned an invalid AgentTask")
        return task
    return AgentTask(
        text=case.input,
        turn_context=_case_context(case),
        metadata={"eval_suite": suite_id, "eval_case": case.case_id},
    )


def _case_context(case: EvalCase) -> str:
    parts = [
        "## Eval Case Context",
        f"case_id: {case.case_id}",
        f"category: {case.category}",
    ]
    if case.context:
        parts.append(f"context: {case.context}")
    return "\n".join(parts)


def _looks_like_path(value: str) -> bool:
    return any(sep in value for sep in ("/", "\\")) or value.endswith((".yaml", ".yml"))


class _EvalWorkspaceEnv:
    def __init__(self, workspace: Workspace) -> None:
        self._values = {
            f"{ENV_PREFIX}_WORKSPACE": str(workspace.root),
            f"{ENV_PREFIX}_CHAT_KIND": workspace.chat_kind or "",
            f"{ENV_PREFIX}_CHAT_ID": workspace.chat_id or "",
            f"{ENV_PREFIX}_USER_ID": workspace.user_id or "",
            f"{ENV_PREFIX}_USER_NAME": workspace.user_name or "",
        }
        self._old: dict[str, str | None] = {}

    def __enter__(self) -> "_EvalWorkspaceEnv":
        for key, value in self._values.items():
            self._old[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        for key, old_value in self._old.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

