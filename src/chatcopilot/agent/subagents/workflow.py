"""Deterministic workflows that call subagents in fixed order with optional retry."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from chatcopilot.agent.subagents.runner import SubagentRuntimeConfig, SubagentRunner
from chatcopilot.agent.subagents.spec import SubagentDef, WorkflowDef
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.agent.trace import current_trace
from chatcopilot.external_tools.shared.tool_spec import ToolDef

_MAX_STEP_SUMMARY_CHARS = 1600
_MAX_PRIOR_SUMMARY_CHARS = 2200
_MAX_LIST_ITEMS = 6


@dataclass(frozen=True)
class WorkflowStepResult:
    name: str
    ok: bool
    summary: str
    outputs: tuple[str, ...]
    is_retry: bool = False


@dataclass(frozen=True)
class WorkflowRunResult:
    ok: bool
    summary: str
    outputs: tuple[str, ...]


class WorkflowRunner:
    def __init__(
        self,
        *,
        runner: SubagentRunner,
        definitions: Mapping[str, SubagentDef],
        configs: Mapping[str, SubagentRuntimeConfig],
        predicates: Mapping[str, Callable[[ToolDef], bool]],
    ) -> None:
        self._runner = runner
        self._definitions = definitions
        self._configs = configs
        self._predicates = predicates

    def run(
        self,
        *,
        session_id: str,
        workflow: WorkflowDef,
        task: TaskPack,
    ) -> WorkflowRunResult:
        parent = current_trace()
        depth = (parent.depth if parent is not None else 0) + 1
        if depth >= workflow.max_depth:
            rejection_payload = {
                "ok": False,
                "summary": f"workflow_depth_limit: {workflow.name} rejected at depth {depth}",
                "steps": [],
                "outputs": [],
                "risks": ["workflow nesting is limited to main -> workflow -> subagent"],
            }
            return WorkflowRunResult(
                ok=False,
                summary=json.dumps(rejection_payload, ensure_ascii=False),
                outputs=(),
            )

        step_results: list[WorkflowStepResult] = []
        prior_summaries: list[str] = []
        outputs: list[str] = []
        retries_used = 0
        steps_list = list(workflow.steps)
        step_idx = 0

        while step_idx < len(steps_list):
            step = steps_list[step_idx]
            definition = self._definitions.get(step)
            config = self._configs.get(step)
            predicate = self._predicates.get(step)
            if definition is None or config is None or predicate is None:
                optional_unavailable = step in workflow.optional_steps
                summary = (
                    f"optional workflow step skipped: {step} unavailable"
                    if optional_unavailable
                    else f"workflow step is not enabled: {step}"
                )
                step_results.append(
                    WorkflowStepResult(
                        name=step,
                        ok=optional_unavailable,
                        summary=summary,
                        outputs=(),
                    )
                )
                if not optional_unavailable:
                    break
                step_idx += 1
                continue

            step_task = _with_prior(task, workflow_name=workflow.name, step_name=step, prior=prior_summaries)
            result = self._runner.run(
                session_id=session_id,
                subagent_name=definition.name,
                task=step_task,
                system_prompt=definition.system_prompt,
                prompt_layers=definition.prompt_layers,
                version=definition.version,
                context_policy=definition.context_policy,
                cache_policy=definition.cache_policy,
                allow_tool=predicate,
                config=config,
                unavailable_message=definition.unavailable_message,
            )
            optional_unavailable = (
                step in workflow.optional_steps
                and result.error_code == f"{step}_unavailable"
            )
            step_ok = True if optional_unavailable else result.ok
            step_summary = (
                f"optional workflow step skipped: {step} unavailable"
                if optional_unavailable
                else _compact_summary(result.summary, max_chars=_MAX_STEP_SUMMARY_CHARS)
            )
            step_results.append(
                WorkflowStepResult(
                    name=step,
                    ok=step_ok,
                    summary=step_summary,
                    outputs=result.outputs,
                )
            )

            if not step_ok:
                # Check if we can retry from an earlier step
                retry_dict = dict(workflow.retry_map)
                retry_target = retry_dict.get(step)
                if retry_target and retries_used < workflow.max_retries:
                    retry_target_idx = _find_step_index(steps_list, retry_target)
                    if retry_target_idx is not None and retry_target_idx < step_idx:
                        retries_used += 1
                        # Inject failure context into prior_summaries for the retry
                        prior_summaries.append(
                            f"RETRY({step} failed → retrying from {retry_target}): "
                            f"{step_summary}"
                        )
                        # Rewind: go back to the retry target
                        step_idx = retry_target_idx
                        continue
                # No retry possible, stop the workflow
                break

            if not optional_unavailable:
                prior_summaries.append(
                    f"{step}: {_compact_summary(result.summary, max_chars=_MAX_PRIOR_SUMMARY_CHARS)}"
                )
                outputs.extend(result.outputs)
            step_idx += 1

        # Determine success: use the LAST result per step name (retry supersedes old failure)
        final_by_step: dict[str, WorkflowStepResult] = {}
        for item in step_results:
            final_by_step[item.name] = item
        ok = all(item.ok for item in final_by_step.values())
        payload: dict[str, Any] = {
            "ok": ok,
            "summary": f"workflow {workflow.name} completed" if ok else f"workflow {workflow.name} completed with issues",
            "steps": [
                {"name": item.name, "ok": item.ok, "summary": item.summary}
                for item in step_results
            ],
            "outputs": list(dict.fromkeys(outputs)),
        }
        if retries_used > 0:
            payload["retries_used"] = retries_used
        return WorkflowRunResult(
            ok=ok,
            summary=json.dumps(payload, ensure_ascii=False),
            outputs=tuple(dict.fromkeys(outputs)),
        )


def _find_step_index(steps: list[str], name: str) -> int | None:
    try:
        return steps.index(name)
    except ValueError:
        return None


def _with_prior(task: TaskPack, *, workflow_name: str, step_name: str, prior: list[str]) -> TaskPack:
    inputs = [
        *task.inputs,
        f"workflow={workflow_name}",
        f"workflow_step={step_name}",
    ]
    if prior:
        inputs.append("prior_step_results:\n" + "\n".join(prior))
    return TaskPack(
        objective=task.objective,
        user_intent=task.user_intent,
        deliverable=task.deliverable,
        constraints=task.constraints,
        inputs=tuple(inputs),
        resources=task.resources,
        acceptance_criteria=task.acceptance_criteria,
        evidence_required=task.evidence_required,
        write_scope=task.write_scope,
        excluded_context=task.excluded_context,
        cache_key_hint=task.cache_key_hint,
        legacy_task=task.legacy_task,
    )


def _compact_summary(summary: str, *, max_chars: int) -> str:
    text = str(summary or "")
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return _truncate(text, max_chars)
    compact = _compact_json(value)
    return _truncate(json.dumps(compact, ensure_ascii=False), max_chars)


def _compact_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in ("ok", "summary", "confidence", "error_code", "limits"):
            if key in value:
                out[key] = _compact_json(value[key])
        for key in ("findings", "evidence", "outputs", "risks", "next_steps"):
            items = value.get(key)
            if isinstance(items, list):
                out[key] = [_compact_json(item) for item in items[:_MAX_LIST_ITEMS]]
                if len(items) > _MAX_LIST_ITEMS:
                    out[f"{key}_omitted"] = len(items) - _MAX_LIST_ITEMS
        return out or {
            key: _compact_json(item)
            for key, item in list(value.items())[:_MAX_LIST_ITEMS]
        }
    if isinstance(value, list):
        return [_compact_json(item) for item in value[:_MAX_LIST_ITEMS]]
    if isinstance(value, str):
        return _truncate(value, 320)
    return value


def _truncate(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


__all__ = ["WorkflowRunner", "WorkflowRunResult"]
