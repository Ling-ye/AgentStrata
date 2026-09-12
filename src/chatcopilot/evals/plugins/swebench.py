"""SWE-bench task and upstream grading inside Core-owned Trial supervision."""

from __future__ import annotations

from chatcopilot.evals.execution_support import cleanup

import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import uuid

from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.evals.adapters import swebench, swebench_runtime as sandbox
from chatcopilot.evals.benchmark_scoring import score_benchmark
from chatcopilot.evals.environment_agent import run_environment_agent
from chatcopilot.evals.execution_support import usage_summary
from chatcopilot.evals.plugins.base import EvaluationPlugin, PLUGIN_API_VERSION
from chatcopilot.evals.trial_capture import record_environment


@contextmanager
def open_case(case, *, bot: str, workspace_root: Path, options: dict[str, Any]):
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
        repository = sandbox.prepare_repository(name, case.metadata["base_commit"])

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
        from chatcopilot.evals.models import TrialObservation
        from chatcopilot.evals.models import PreparedCase
        metadata = {"predicted_patch": patch, "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                    "image_id": image_id, "repository_preparation": repository,
                    "execution_protocol": "network-disabled capped container; upstream log grading"}
        observed = TrialObservation(final_text=final_text, stop_reason="end_turn", events=tuple(events),
            tool_calls=tuple(audit), usage=usage_summary(events).get("usage_totals", {}),
            evidence=({"kind": "adapter_metadata", **metadata},))
        def assess():
            nonlocal name
            cleanup(lambda: sandbox.cleanup_container(name))
            name = f"agentstrata-swe-{run_id}-grade"
            record_environment({"kind": "swe-bench", "container": name})
            sandbox.start_container(name, image_id)
            sandbox.prepare_repository(name, case.metadata["base_commit"])
            grading = {}
            def native():
                nonlocal grading
                result, grading = sandbox.grade(case, patch, container=name, output=workspace_root)
                return result
            judge, evidence = score_benchmark("swe-bench-verified", case, final_text, native, options=options,
                                             tool_calls=[{"name": "predicted_patch", "result": patch}])
            return judge, {**evidence, "environment_result": grading}
        yield PreparedCase(observed, assess)
    finally:
        if lease_active:
            cleanup(lambda: sandbox.cleanup_container(name))


PLUGIN = EvaluationPlugin(plugin_id="swe-bench", api_version=PLUGIN_API_VERSION,
    implementation_module=__name__, allowed_drivers=frozenset({"agent_configured", "dry_run"}),
    load_cases=lambda context: swebench.load_cases(), preflight=sandbox.preflight, open_case=open_case)
