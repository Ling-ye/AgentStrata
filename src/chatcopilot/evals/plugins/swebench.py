"""SWE-bench task and upstream grading inside Core-owned Trial supervision."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any
import uuid

from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.evals.adapters import swebench, swebench_runtime as sandbox
from chatcopilot.evals.benchmark_scoring import score_benchmark
from chatcopilot.evals.environment_agent import run_environment_agent
from chatcopilot.evals.execution_support import usage_summary
from chatcopilot.evals.models import EvalCaseResult
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.trial_capture import capture_case, record_environment


@capture_case
def _execute(case, *, bot: str, workspace_root: Path, options: dict[str, Any]) -> EvalCaseResult:
    started = time.monotonic()
    run_id = uuid.uuid4().hex
    name = f"agentstrata-swe-{run_id}-solve"
    final_text = ""
    lease_active = False
    events: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    try:
        sandbox.preflight(cases=[case])
        lease_active = True
        record_environment({"kind": "swe-bench", "container": name})
        image_id = sandbox.start_container(name, case.metadata["swe_instance"]["image"])
        _, base = sandbox.docker(["exec", "--workdir", "/testbed", name, "git", "rev-parse", "HEAD"])
        if base.strip() != case.metadata["base_commit"]:
            raise ValueError("SWE-bench image base commit differs from the frozen case")

        def shell(arguments, context):
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip() or len(command) > 32000:
                return ToolResult(ok=False, data={"error": "command must be bounded text"})
            if len(audit) >= 80:
                raise ValueError("SWE-bench action budget exhausted")
            code, text = sandbox.docker(["exec", "--workdir", "/testbed", name, "/bin/bash", "-lc", command],
                                        timeout=120, limit=128 * 1024, check=False)
            result = {"exit_code": code, "output": text}
            audit.append({"name": "benchmark_shell", "arguments": {"command": command}, "result": result})
            return ToolResult(ok=code == 0, data=result)

        tool = ToolDef(name="benchmark_shell", summary="Execute a shell command in the isolated benchmark repository at /testbed. Use this tool to inspect, edit and test code. The local Agent workspace is not the target repository.",
            input_schema=object_schema({"command": {"type": "string"}}, required=("command",)),
            output_schema=object_schema({"exit_code": {"type": "integer"}, "output": {"type": "string"}}, required=("exit_code", "output")), handler=shell, category="eval.environment", owner="evals")
        provider = ToolProvider(id="evals.swebench", module=__name__, packs={"runtime.session": (tool,)},
                                description="Disposable SWE-bench repository")
        final_text, events = run_environment_agent(bot=bot, workspace_root=workspace_root / "agent",
            task_text=case.input, provider=provider, tool_names=frozenset({tool.name}))
        patch = sandbox.read_patch(name)
        sandbox.cleanup_container(name)
        name = f"agentstrata-swe-{run_id}-grade"
        record_environment({"kind": "swe-bench", "container": name})
        sandbox.start_container(name, image_id)
        grading: dict[str, Any] = {}

        def native():
            nonlocal grading
            result, grading = sandbox.grade(case, patch, container=name, output=workspace_root)
            return result

        judge, evidence = score_benchmark("swe-bench-verified", case, final_text, native, options=options,
                                         tool_calls=[{"name": "predicted_patch", "result": patch}])
        return EvalCaseResult(case_id=case.case_id, suite_id="swe-bench-verified", status="passed" if judge.passed else "failed",
            score=judge.score, max_score=judge.max_score, final_text=final_text, judge=judge,
            duration_seconds=time.monotonic() - started, events=tuple(events),
            metadata={**usage_summary(events), "judge_evidence": evidence, "tool_calls": audit,
                      "predicted_patch": patch, "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                      "image_id": image_id, "environment_result": grading,
                      "execution_protocol": "network-disabled capped container; upstream log grading"})
    except Exception as exc:
        return EvalCaseResult(case_id=case.case_id, suite_id="swe-bench-verified", status="error", final_text=final_text,
            events=tuple(events), duration_seconds=time.monotonic() - started, error=str(exc), metadata={"tool_calls": audit})
    finally:
        if lease_active:
            sandbox.cleanup_container(name)


PLUGIN = EvaluationPlugin(plugin_id="swe-bench", api_version=PLUGIN_API_VERSION,
    implementation_module=__name__, allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=lambda context: swebench.load_cases(), preflight=sandbox.preflight, execute_trial=_execute)
