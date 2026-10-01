"""Confined coding adapter, without chat jobs or Git publication behavior."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from chatcopilot.agent.runtimes.codex_permissions import permission_config
from chatcopilot.agent.context.prompt_plan import (
    PromptBuildInput,
    PromptPlanBuilder,
    render_codex_prompt,
    render_codex_developer,
)
from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.core.observability_redaction import redact_observability_payload
from chatcopilot.core.private_sqlite import json_text, private_directory
from chatcopilot.core.source_snapshot import git_output
from chatcopilot.core.scoped_process import require_bubblewrap
from chatcopilot.harness.codex_environment import check_git, git_metadata, shell_environment, wrap_command
from chatcopilot.external_tools.codex_cli import (
    build_codex_subprocess_env,
    credential_lease,
    validate_auth_root_path,
)
from chatcopilot.external_tools.codex_cli import build_app_server_command
from chatcopilot.harness.models import HarnessError, RepairOptions, CodingOptions, review_decision
from chatcopilot.harness.evidence_context import evidence_index
from chatcopilot.harness.workspace import protected_paths, writable_paths
from chatcopilot.harness.repair_types import ActionProgress
from chatcopilot.harness.agent_types import AgentCall, AgentResult, Role
from chatcopilot.harness.agent_execution import execute_agent_call
from chatcopilot.harness.role_prompts import COMMON, PROMPTS, GOVERNANCE_PROMPTS, SKILL_LEARNING_PROMPTS
from chatcopilot.harness.repair_session import run_session


class CodexCoder:
    def __init__(self, trace_publisher: Callable[[Path, dict[str, Any]], None] | None = None) -> None:
        self.trace_publisher = trace_publisher

    def preflight(self, model_config=None) -> tuple[Path, Path]:
        require_bubblewrap()
        raw = model_config.codex_bin if model_config is not None else os.environ.get("CHATCOPILOT_CODEX_BIN", "")
        if not raw or not Path(raw).is_absolute():
            raise HarnessError(
                "codex_unconfigured", "请配置 CHATCOPILOT_CODEX_BIN 为原生 Codex 可执行文件"
            )
        binary = Path(raw).resolve(strict=True)
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise HarnessError("codex_unavailable", "Codex 可执行文件不可用")
        with binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise HarnessError(
                    "codex_unavailable", "独立 worker 需要 Linux 原生 Codex 二进制文件"
                )
        auth = validate_auth_root_path(model_config.credential_root if model_config is not None else os.environ.get("CHATCOPILOT_CODEX_BOT_HOME", ""))
        return binary, auth

    def execute(self, worktree: Path, call: AgentCall, options: RepairOptions | CodingOptions,
                output: Path, cancel: Callable[[], None]) -> AgentResult:
        return execute_agent_call(
            worktree, call, output,
            lambda: self._execute_impl(worktree, call, options, output, cancel),
            trace_publisher=self.trace_publisher,
        )

    def review(self, worktree, evidence, options, output, check_cancel):
        call = AgentCall(evidence["task_id"], Role.REVIEW, 1, "独立审查精确候选", {**evidence, "base_commit": git_output(worktree, "rev-parse", "HEAD")})
        result = self.execute(worktree, call, options, output, check_cancel)
        standard = {key: result.payload[key] for key in ("decision", "problem", "reason", "evidence_refs")}
        return {**result.payload, **review_decision(standard), "execution": result.execution}

    def _execute_impl(self, worktree: Path, call: AgentCall, options, output: Path, check_cancel):
        if options.model_settings:
            from chatcopilot.core.model_settings import resolve_profile
            cfg = resolve_profile(options.model_profile, document=options.model_settings)
            binary, auth = self.preflight(cfg)
            if (cfg.model, cfg.reasoning_effort or "") != (options.model, options.reasoning_effort):
                raise HarnessError("model_configuration", "冻结模型与任务参数不一致")
        else:
            binary, auth = self.preflight()
        role, evidence = call.role, call.evidence
        task_root = worktree.parent
        private_directory(output)
        learning = bool(call.evidence.get("source", {}).get("skill_learning"))
        draft = private_directory(output / "draft") if role == Role.TEST or (learning and role == Role.CODING) else None
        evidence_text = json_text(evidence)
        evidence_bytes = len(evidence_text.encode())
        evidence_path = private_directory(output / "evidence") / "evidence.json"
        fd = os.open(evidence_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(evidence_text)
        evidence_text = json_text({"task_id": call.task_id, "role": role.value, "revision": call.revision,
            "goal": call.goal, "evidence_file": str(evidence_path),
            "sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
            **evidence_index(evidence, stage="review" if role == Role.REVIEW else "prepare" if role in {Role.MAIN, Role.PLAN, Role.TEST} else "repair")})
        runtime_home = private_directory(task_root / "sessions" / role.value)
        execution_directory = worktree
        source = evidence.get("source", {})
        governance = source.get("kind") == "code_health"
        protected = protected_paths(worktree, str(source.get("bot_id", "")),
                                    governance=governance, learning=learning)
        helper_directory = private_directory(task_root / "sessions" / "bin")
        alias = helper_directory / "codex-linux-sandbox"
        if alias.is_symlink():
            if alias.resolve() != binary:
                raise HarnessError("coding_environment", "任务沙箱执行器身份变化")
        elif alias.exists():
            raise HarnessError("coding_environment", "任务沙箱执行器路径被占用")
        else:
            alias.symlink_to(binary)
        rg_executable = shutil.which("rg")
        rg = Path(rg_executable).resolve() if rg_executable else None
        git_roots = git_metadata(worktree)
        tool_environment = shell_environment(binary, helper_directory)
        tool_environment["PYTHONPATH"] = str(worktree / "src") + os.pathsep + str(worktree)
        task_python = task_root / "environment/venv/bin/python"
        if task_python.is_file():
            tool_environment["PATH"] = str(task_python.parent) + ":" + tool_environment["PATH"]
        runtime_identity = hashlib.sha256(json_text({"binary": str(binary), "size": binary.stat().st_size,
            "modified_ns": binary.stat().st_mtime_ns, "python": sys.version, "executable": sys.executable,
            "shell": tool_environment, "role": role.value,
            "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}).encode()).hexdigest()
        readable = [worktree, binary.parent, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                    *git_roots, helper_directory, evidence_path.parent]
        for name in ("artifacts", "snapshots", "environment"):
            if (task_root / name).exists():
                readable.append(task_root / name)
        for ref in (source.get("trace_archive"), source.get("test_path"),
                    source.get("baseline_root"), evidence.get("baseline_root")):
            if ref:
                path = Path(ref)
                if path.resolve() != path or not path.is_relative_to(task_root):
                    raise HarnessError("artifact_changed", "材料位置超出本任务")
                readable.append(path)
        if draft:
            readable.append(draft)
        writes = ((draft,) if learning else writable_paths(worktree, str(source.get("bot_id", "")),
                                governance=governance, learning=False)) if role == Role.CODING else (draft,) if draft else ()
        scope = ExecutionScope(readable_roots=tuple(dict.fromkeys(readable)), writable_roots=writes,
                               protected_roots=(*protected, *git_roots), native_write=bool(writes))
        profile = BotPromptProfile(identity="AgentStrata Harness " + role.value, response_style="报告有证据的结论和缺口。")
        instructions = (SKILL_LEARNING_PROMPTS[role] if source.get("skill_learning")
                        else PROMPTS[role] + (GOVERNANCE_PROMPTS.get(role, "") if governance else ""))
        if evidence.get("harness_skill"):
            instructions += "本轮宿主已加载冻结的 Harness Skill。显式使用 $harness-code-health，并按当前角色职责读取所需参考资料。"
        plan = PromptPlanBuilder().build(PromptBuildInput(profile=profile, runtime_id="codex", model=options.model,
            role="owner", channel_kind="private", session_policy=COMMON + instructions))
        prompt = render_codex_prompt(plan, user_message=instructions +
                                     (f" draft={draft} target=evidence.md" if learning and role == Role.CODING else
                                      f" draft={draft}" if draft else ""),
                                     turn_context=evidence_text, trusted_separately=True)
        events = []
        progress = ActionProgress()
        from chatcopilot.core.trace_capture import current_capture
        capture = current_capture()
        if capture:
            capture.record({"kind": "coding_request", "status": "recorded", "data": {"model": options.model}},
                           {"prompt": prompt, "developer_instructions": render_codex_developer(plan), "source_trace": source.get("trace"),
                            "coverage": "adapter_visible", "omitted": ["provider_internal_context"]})
        usage: dict[str, Any] = {}
        final_text = ""
        context_metrics = {"prompt_bytes": len(prompt.encode()), "evidence_bytes": evidence_bytes,
            "source_index_bytes": len(json_text(evidence.get("source_index")).encode()) if evidence.get("source_index") else 0,
            "failure_brief_bytes": len(json_text(evidence.get("failure_brief")).encode()) if evidence.get("failure_brief") else 0,
            "command_count": 0, "command_output_chars": 0, "max_command_output_chars": 0,
            "truncated_command_count": 0, "context_budget_warning": []}
        native_scope = replace(scope, protected_roots=(*scope.protected_roots, runtime_home))
        if any(path.is_file() for path in scope.writable_roots):
            # Codex writable roots require directories. The outer namespace
            # keeps the original exact file mounts and read-only parent, while
            # the native child protects Git and the parent's runtime state.
            # No vendor-specific mountpoints are created in the candidate.
            native_scope = replace(
                native_scope,
                readable_roots=(),
                writable_roots=(Path("/"),),
            )
        config = permission_config(
            native_scope, workdir=execution_directory, private_paths=(str(runtime_home / "auth.json"),), network_access=False
        )
        if git_roots:
            preflight_config = permission_config(
                native_scope, workdir=execution_directory, private_paths=(), network_access=False)
            check_git(binary, scope=scope, cwd=execution_directory, root=worktree, metadata=git_roots,
                      expected_head=evidence["base_commit"], runtime_home=runtime_home, config=preflight_config,
                      environment=tool_environment, rg=rg, timeout=options.timeout_seconds, check_cancel=check_cancel)
        with credential_lease(auth, "worker", runtime_home, blocking=False) as lease:
            command = build_app_server_command(shlex.quote(str(binary)) + " exec",
                model=options.model, workdir=execution_directory, reasoning_effort=options.reasoning_effort,
                web_search_mode="disabled", shell_env_overrides=tool_environment,
                extra_config=(*config, "mcp_servers={}", "features.hooks=false", "features.apps=false",
                              "features.image_generation=false", "features.multi_agent=false"))
            environment = build_codex_subprocess_env(str(binary), runtime_home=runtime_home)
            bindings = ["--dir", str(output), "--dir", str(runtime_home.parent), "--bind", str(runtime_home), str(runtime_home)]
            for name in (
                "CODEX_HOME",
                "CODEX_SQLITE_HOME",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
            ):
                if environment.get(name):
                    bindings.extend(["--setenv", name, environment[name]])
            outer = wrap_command(command, scope=scope, cwd=execution_directory,
                                 environment=tool_environment, rg=rg, bindings=tuple(bindings))
            log_path = output / "public-events.jsonl"
            fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as stream:

                def observe(line: str) -> None:
                    nonlocal final_text
                    try:
                        event = json.loads(line)
                    except ValueError:
                        return
                    if event.get("type") == "turn.completed":
                        usage.update(event.get("usage") or {})
                    item = event.get("item") or {}
                    if event.get("type") == "item.completed":
                        progress.observe(item)
                    if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                        command_output = str(item.get("aggregated_output", ""))
                        context_metrics["command_count"] += 1
                        context_metrics["command_output_chars"] += len(command_output)
                        context_metrics["max_command_output_chars"] = max(
                            context_metrics["max_command_output_chars"], len(command_output))
                        if "chars omitted by Harness" in command_output:
                            context_metrics["truncated_command_count"] += 1
                    if event.get("type") != "item.completed" or item.get("type") not in {
                        "agent_message",
                        "command_execution",
                    }:
                        return
                    projected = {
                        key: item[key]
                        for key in ("type", "text", "command", "aggregated_output", "exit_code")
                        if key in item
                    }
                    safe = redact_observability_payload(projected).value
                    if capture:
                        capture.record({"kind": str(item.get("type")),
                                        "status": "failed" if item.get("exit_code") else "recorded"}, projected)
                    if item.get("type") == "agent_message":
                        final_text = str(item.get("text", ""))
                    stream.write(json_text(safe) + "\n")
                    stream.flush()
                    if len(events) < 50:
                        events.append(safe)

                session = run_session(outer, root=execution_directory, home=runtime_home, environment=environment,
                    prompt=prompt, options=options, task_id=call.task_id, generation=lease.generation,
                    role=role, attempt=call.revision, observe=observe, cancel=check_cancel,
                    developer_instructions=render_codex_developer(plan), environment_identity=runtime_identity,
                    governance=governance)
        warnings = context_metrics["context_budget_warning"]
        if role == Role.PLAN and context_metrics["command_output_chars"] > 64 * 1024:
            warnings.append("plan_command_output")
        if role == Role.PLAN and context_metrics["max_command_output_chars"] > 32 * 1024:
            warnings.append("plan_single_command_output")
        if role == Role.PLAN and usage.get("input_tokens", 0) > 120_000:
            warnings.append("plan_input_tokens")
        if evidence.get("source_index", {}).get("limits", {}).get("truncated"):
            warnings.append("source_index_truncated")
        if evidence.get("failure_brief", {}).get("limits", {}).get("truncated"):
            warnings.append("failure_brief_truncated")
        return {"events": events, "usage": usage, "context_metrics": context_metrics,
                "log": log_path.name, "final_text": final_text, "session": session}


def require_available_model(models: list[dict], model: str, effort: str) -> None:
    entry = next((row for row in models if row.get("model", row.get("id")) == model), None)
    if entry is None:
        raise HarnessError("model_unavailable", f"Harness worker 当前不可使用模型 {model}；请核对 Codex CLI 版本和 worker 凭据")
    efforts = entry.get("supportedReasoningEfforts")
    if effort and not isinstance(efforts, list):
        raise HarnessError("model_probe_unavailable", "Codex 模型目录缺少推理强度信息")
    if effort and effort not in {row.get("reasoningEffort") for row in efforts if isinstance(row, dict)}:
        raise HarnessError("model_effort_unsupported", f"Harness worker 的模型 {model} 不支持推理强度 {effort}")


def worker_models(settings: Mapping[str, str], repository: Path) -> list[dict]:
    from chatcopilot.external_tools.codex_cli import codex_models
    try:
        return codex_models({"kind": "codex", "auth": {"mode": "chatgpt", "profile": "worker"}}, settings, repository)
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise HarnessError("model_probe_unavailable", "无法查询 worker 的模型目录；请检查 Codex、凭据与网络") from exc


def preflight_worker_model(settings: Mapping[str, str], repository: Path, model: str, effort: str,
                           *, model_settings=None, model_profile="") -> None:
    if model_settings:
        from chatcopilot.core.model_settings import resolve_profile
        cfg = resolve_profile(model_profile, document=model_settings, environment={**os.environ, **settings})
        settings = {**settings, "CHATCOPILOT_CODEX_BIN": cfg.codex_bin, "CHATCOPILOT_CODEX_BOT_HOME": cfg.credential_root}
    require_available_model(worker_models(settings, repository), model, effort)
