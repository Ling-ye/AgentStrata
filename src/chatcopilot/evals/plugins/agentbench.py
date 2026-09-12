"""Trusted AgentBench FC session adapter."""

from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any

from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.evals.adapters import agentbench
from chatcopilot.evals.benchmark_scoring import score_benchmark
from chatcopilot.evals.environment_agent import run_environment_agent
from chatcopilot.evals.execution_support import usage_summary
from chatcopilot.evals.models import EvalCaseResult
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.trial_capture import capture_case, record_environment


def _preflight(*, cases) -> None:
    agentbench.controller_url()
    if not cases:
        raise ValueError("请准备带 task/index/input/source_revision 的 AgentBench FC 本地题目目录")
    unavailable = [state["reason"] for state in agentbench.case_readiness(tuple(cases)).values() if not state["ready"]]
    if unavailable:
        raise ValueError("；".join(dict.fromkeys(unavailable)))


@capture_case
def _execute(case, *, bot: str, workspace_root: Path, options: dict[str, Any]) -> EvalCaseResult:
    started = time.monotonic()
    _preflight(cases=(case,))
    controller = agentbench.Controller()
    final_text = ""
    events: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    try:
        observation = controller.start(case)
        record_environment({"kind": "agentbench-fc", "session_id": controller.session_id,
                            "controller_fingerprint": hashlib.sha256(controller.url.encode()).hexdigest()})
        tools = []
        for entry in observation.get("tools") or []:
            function = entry.get("function", {})
            name = function.get("name", "")
            schema = function.get("parameters", {})
            if not isinstance(name, str) or not name or not isinstance(schema, dict):
                raise ValueError("invalid AgentBench tool definition")

            def make_handler(tool_name):
                def handler(arguments, context):
                    nonlocal observation
                    if observation["finish"]:
                        return ToolResult(ok=False, data={"error": "environment already completed"})
                    if len(audit) >= 50:
                        raise ValueError("AgentBench tool budget exhausted")
                    observation = controller.call(tool_name, dict(arguments), f"call_{len(audit)}")
                    visible = {key: observation.get(key) for key in ("messages", "finish")}
                    audit.append({"name": tool_name, "arguments": dict(arguments), "result": visible})
                    return ToolResult(ok=True, data=visible)
                return handler

            tools.append(ToolDef(name=name, summary=str(function.get("description", "")),
                input_schema=schema, output_schema=object_schema({"messages": {"type": ["array", "null"]}, "finish": {"type": "boolean"}}, required=("finish",)), handler=make_handler(name),
                category="eval.environment", owner="evals"))
        if not tools and not observation["finish"]:
            raise ValueError("AgentBench environment supplied no callable tools")
        provider = ToolProvider(id="evals.agentbench", module=__name__, packs={"runtime.session": tuple(tools)},
                                description="Isolated AgentBench environment tools")
        prompt = json.dumps(observation.get("messages", []), ensure_ascii=False)
        if not observation["finish"]:
            final_text, events = run_environment_agent(bot=bot, workspace_root=workspace_root,
                task_text=prompt, provider=provider, tool_names=frozenset(t.name for t in tools))
        judge, evidence = score_benchmark("agentbench-fc", case, final_text,
            lambda: agentbench.judge_response(observation), options=options, tool_calls=audit)
        return EvalCaseResult(case_id=case.case_id, suite_id="agentbench-fc", status="passed" if judge.passed else "failed",
            score=judge.score, max_score=judge.max_score, final_text=final_text, judge=judge,
            duration_seconds=time.monotonic() - started, events=tuple(events),
            metadata={**usage_summary(events), "judge_evidence": evidence, "tool_calls": audit,
                      "environment_result": {key: observation.get(key) for key in ("finish", "status", "reward", "metrics")}})
    except Exception as exc:
        return EvalCaseResult(case_id=case.case_id, suite_id="agentbench-fc", status="error", final_text=final_text,
            events=tuple(events), duration_seconds=time.monotonic() - started, error=str(exc), metadata={"tool_calls": audit})
    finally:
        controller.close()


PLUGIN = EvaluationPlugin(plugin_id="agentbench-fc", api_version=PLUGIN_API_VERSION,
    implementation_module=__name__, allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=lambda context: agentbench.load_cases(), preflight=_preflight, execute_trial=_execute)
