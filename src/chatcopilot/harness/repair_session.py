"""Task-bound native Codex conversations, isolated from candidate shell state."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.external_tools.codex_cli import run_app_server
from chatcopilot.harness.models import HarnessError, PIPELINE_VERSION
from chatcopilot.harness.agent_types import Role, role_schema
from chatcopilot.harness.agent_session_state import open_role_session


_MAX_OBSERVED_COMMAND_CHARS = 64 * 1024


def _unsupported_model(error: Any, model: str) -> bool:
    if not isinstance(error, dict):
        return False
    try:
        details = json.loads(error.get("message", ""))
    except (TypeError, ValueError):
        return False
    if not isinstance(details, dict) or details.get("status") != 400:
        return False
    body = details.get("error") or {}
    return (isinstance(body, dict) and body.get("type") == "invalid_request_error"
            and f"'{model}' model is not supported when using Codex with a ChatGPT account" in str(body.get("message", "")))


def _bounded_command_output(value: Any) -> str:
    text = str(value or "")
    if len(text) <= _MAX_OBSERVED_COMMAND_CHARS:
        return text
    half = _MAX_OBSERVED_COMMAND_CHARS // 2
    omitted = len(text) - half * 2
    return text[:half] + f"\n...[{omitted} chars omitted by Harness]...\n" + text[-half:]



def run_session(command, *, root: Path, home: Path, environment: dict[str, str], prompt: str,
                options, task_id: str, generation: int, role: Role,
                observe: Callable[[str], None], cancel: Callable[[], None],
                developer_instructions: str | None = None, environment_identity: str = "",
                governance: bool = False, attempt: int = 1) -> dict[str, Any]:
    session = open_role_session(
        home=home, task_id=task_id, worktree=root,
        model=options.model, effort=options.reasoning_effort,
        credential_generation=generation, pipeline_version=PIPELINE_VERSION,
        role=role, environment_identity=environment_identity,
        attempt=attempt, governance=governance,
    )
    state = session.value
    thread_id = session.previous_thread_id
    initial_usage = session.initial_usage

    def thread(ident):
        session.record_thread(ident)

    turn_status = None
    observed_thread = thread_id

    def notification(method, params):
        nonlocal turn_status
        if params.get("threadId") != state.get("thread_id"):
            return
        if method == "turn/started":
            session.record_turn_started()
        if method == "thread/tokenUsage/updated":
            total = params.get("tokenUsage", {}).get("total", {})
            fields = {"inputTokens": "input_tokens", "outputTokens": "output_tokens", "totalTokens": "total_tokens",
                      "cachedInputTokens": "cached_input_tokens", "reasoningOutputTokens": "reasoning_output_tokens"}
            usage = {target: total[key] - initial_usage.get(key, 0) for key, target in fields.items()
                     if type(total.get(key)) is int and total[key] >= initial_usage.get(key, 0)}
            state["usage_totals"] = total
            observe(json_text({"type": "turn.completed", "usage": usage}))
        if method == "turn/completed":
            turn_status = params.get("turn", {}).get("status")
            error = params.get("turn", {}).get("error")
            if error:
                from chatcopilot.harness.config import safe_error
                state["error"] = safe_error(Exception(str(error)))
                if _unsupported_model(error, options.model):
                    state["error_code"] = "model_unavailable"
        if method != "item/completed":
            return
        item = params.get("item") or {}
        if item.get("type") == "agentMessage":
            observe(json_text({"type": "item.completed", "item": {"type": "agent_message", "text": item.get("text", "")}}))
        elif item.get("type") == "commandExecution":
            observe(json_text({"type": "item.completed", "item": {"type": "command_execution",
                "command": item.get("command", ""),
                "aggregated_output": _bounded_command_output(item.get("aggregatedOutput", "")),
                "exit_code": item.get("exitCode")}}))
        elif item.get("type") == "fileChange":
            observe(json_text({"type": "item.completed", "item": {"type": "file_change", "changes": item.get("changes", [])}}))

    try:
        run_app_server(command, cwd=root, env=environment, prompt=prompt, model=options.model,
            effort=options.reasoning_effort, thread_id=thread_id, image_paths=(),
            timeout_seconds=options.timeout_seconds, on_notification=notification,
            on_thread=thread, on_poll=cancel, output_schema=role_schema(role, governance=governance),
            developer_instructions=developer_instructions)
        if turn_status != "completed":
            raise HarnessError(state.get("error_code", "coding_environment"), "修复会话未成功完成"
                               + (": " + state["error"] if state.get("error") else ""))
    except BaseException:
        if not observed_thread and not state.get("accepted"):
            state["thread_id"] = ""
        session.finish(completed=False, terminal_known=turn_status in {"completed", "failed", "interrupted"})
        raise
    session.finish(completed=True, terminal_known=True)
    return state
