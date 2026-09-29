"""Durable Harness role-session binding, independent of the native agent protocol."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text, private_file
from chatcopilot.harness.agent_types import Role
from chatcopilot.harness.models import HarnessError


@dataclass
class AgentSessionState:
    path: Path
    value: dict[str, Any]
    previous_thread_id: str
    initial_usage: dict[str, int]

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json_text(self.value))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def record_thread(self, native_id: str) -> None:
        self.value["thread_id"] = native_id
        self.save()

    def record_turn_started(self) -> None:
        self.value["accepted"] = True
        self.save()

    def finish(self, *, completed: bool, terminal_known: bool) -> None:
        self.value["state"] = "completed" if completed else "interrupted" if terminal_known else "uncertain"
        self.save()


def open_role_session(
    *,
    home: Path,
    task_id: str,
    worktree: Path,
    model: str,
    effort: str,
    credential_generation: int,
    pipeline_version: int,
    role: Role,
    environment_identity: str,
    attempt: int,
    governance: bool,
) -> AgentSessionState:
    """Bind a role to one task, workspace, model and credential generation.

    Keep the existing on-disk keys so in-progress Codex work remains resumable.
    The native thread id is opaque to the Harness; the provider driver interprets it.
    """
    path = home / "repair-session.json"
    binding: dict[str, Any] = {
        "task_id": task_id,
        "worktree": str(worktree),
        "model": model,
        "effort": effort,
        "credential_generation": credential_generation,
        "pipeline": pipeline_version,
        "role": role.value,
        "environment_identity": environment_identity,
        "attempt": attempt,
    }
    if governance:
        binding["purpose"] = "governance"
    previous: dict[str, Any] = {}
    if path.exists():
        private_file(path)
        previous = json.loads(path.read_text())
        previous_binding = previous.get("binding") or {}
        stable = {key: value for key, value in binding.items() if key != "attempt"}
        previous_stable = {key: value for key, value in previous_binding.items() if key != "attempt"}
        if previous_stable != stable:
            raise HarnessError("session_changed", "修复会话的任务、源码位置、环境、模型或凭据身份变化")
        if previous_binding.get("attempt") == attempt and previous.get("state") in {"running", "uncertain"}:
            raise HarnessError("session_unconfirmed", "本轮会话终态未确认；保留候选，不自动重放")
        if previous_binding.get("attempt") != attempt:
            previous = {}
    thread_id = "" if role == Role.REVIEW else previous.get("thread_id", "")
    initial_usage = {} if role == Role.REVIEW else previous.get("usage_totals", {})
    session = AgentSessionState(
        path=path,
        value={"binding": binding, "thread_id": thread_id, "state": "running"},
        previous_thread_id=thread_id,
        initial_usage=initial_usage,
    )
    session.save()
    return session
