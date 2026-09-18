"""Confined coding adapter, without chat jobs or Git publication behavior."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
import shutil
import sys
import uuid
import logging
from pathlib import Path
from typing import Any, Callable

from chatcopilot.agent.backends.codex_permissions import permission_config
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
from chatcopilot.harness.agent_types import AgentCall, AgentResult, Role, role_result
from chatcopilot.harness.role_prompts import COMMON, PROMPTS
from chatcopilot.harness.repair_session import run_session


class CodexCoder:
    def __init__(self, trace_publisher: Callable[[Path, dict[str, Any]], None] | None = None) -> None:
        self.trace_publisher = trace_publisher

    def preflight(self) -> tuple[Path, Path]:
        require_bubblewrap()
        raw = os.environ.get("CHATCOPILOT_CODEX_BIN", "")
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
        auth = validate_auth_root_path(os.environ.get("CHATCOPILOT_CODEX_BOT_HOME", ""))
        return binary, auth

    def execute(self, worktree: Path, call: AgentCall, options: RepairOptions | CodingOptions,
                output: Path, cancel: Callable[[], None]) -> AgentResult:
        from chatcopilot.core.trace_capture import TraceCapture, capture_scope
        from chatcopilot.core.trace_archive import TraceArchive
        from chatcopilot.harness.flow_records import step_binding
        capture = TraceCapture({"kind": "harness", "execution_id": uuid.uuid4().hex, **step_binding(),
                                "phase": call.role.value, "role": call.role.value},
                               roots={"workspace": worktree, "output": output})
        execution = {}
        status = "failed"
        try:
            with capture_scope(capture):
                execution = self._execute_impl(worktree, call, options, output, cancel)
                payload = role_result(call.role, json.loads(execution.pop("final_text")))
            status = "completed"
            return AgentResult(payload, execution)
        except (ValueError, TypeError) as exc:
            raise HarnessError("invalid_role_result", "角色输出不是有效结构化产物") from exc
        finally:
            try:
                execution["trace"] = TraceArchive(output / "traces").save(capture, status, retained=True)
                if self.trace_publisher:
                    self.trace_publisher(output / "traces", execution["trace"])
            except Exception:
                logging.getLogger(__name__).warning("Harness role trace archive failed")

    def review(self, worktree, evidence, options, output, check_cancel):
        call = AgentCall(evidence["task_id"], Role.REVIEW, 1, "独立审查精确候选", {**evidence, "base_commit": git_output(worktree, "rev-parse", "HEAD")})
        result = self.execute(worktree, call, options, output, check_cancel)
        return {**review_decision(result.payload), "execution": result.execution}

    def _execute_impl(self, worktree: Path, call: AgentCall, options, output: Path, check_cancel):
        binary, auth = self.preflight()
        role, evidence = call.role, call.evidence
        task_root = worktree.parent
        private_directory(output)
        draft = private_directory(output / "draft") if role == Role.TEST else None
        evidence_text = json_text(evidence)
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
        protected = protected_paths(worktree, str(source.get("bot_id", "")))
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
        for ref in (source.get("trace_archive"), source.get("test_path")):
            if ref:
                path = Path(ref)
                if path.resolve() != path or not path.is_relative_to(task_root):
                    raise HarnessError("artifact_changed", "材料位置超出本任务")
                readable.append(path)
        if draft:
            readable.append(draft)
        writes = writable_paths(worktree, str(source.get("bot_id", ""))) if role == Role.CODING else (draft,) if draft else ()
        scope = ExecutionScope(readable_roots=tuple(dict.fromkeys(readable)), writable_roots=writes,
                               protected_roots=(*protected, *git_roots), native_write=bool(writes))
        profile = BotPromptProfile(identity="AgentStrata Harness " + role.value, response_style="报告有证据的结论和缺口。")
        plan = PromptPlanBuilder().build(PromptBuildInput(profile=profile, backend="codex", model=options.model,
            role="owner", channel_kind="private", session_policy=COMMON + PROMPTS[role]))
        prompt = render_codex_prompt(plan, user_message=PROMPTS[role] + (f" draft={draft}" if draft else ""),
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
        config = permission_config(
            scope, workdir=execution_directory, private_paths=(str(runtime_home / "auth.json"), str(runtime_home / "config.toml")), network_access=False
        )
        if git_roots:
            check_git(binary, scope=scope, cwd=execution_directory, root=worktree, metadata=git_roots,
                      expected_head=evidence["base_commit"], runtime_home=runtime_home, config=config,
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
                    role=role, observe=observe, cancel=check_cancel,
                    developer_instructions=render_codex_developer(plan), environment_identity=runtime_identity)
        return {"events": events, "usage": usage, "log": log_path.name, "final_text": final_text, "session": session}
