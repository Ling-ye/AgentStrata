"""Application bootstrap for the per-instance code-task recovery service."""
from __future__ import annotations

import os

from chatcopilot.botspec.runtime import load_runtime_context
from chatcopilot.botspec.runtime_env import apply_runtime_env
from chatcopilot.core.config import load_config
from chatcopilot.core.model_selection import code_task_model_selection
from chatcopilot.external_tools.dev.code_task_service import main as run_service
from chatcopilot.project import ENV_PREFIX


def main(argv: list[str] | None = None) -> int:
    runtime = load_runtime_context()
    configured_instance = os.environ.get(f"{ENV_PREFIX}_INSTANCE_ID", "").strip()
    if not configured_instance or runtime.instance_id != configured_instance:
        raise RuntimeError("code-worker BotSpec does not match CHATCOPILOT_INSTANCE_ID")
    apply_runtime_env(runtime)
    model_env = f"{ENV_PREFIX}_CODE_MODEL"
    effort_env = f"{ENV_PREFIX}_CODE_REASONING_EFFORT"
    if "dev.code_tasks" in runtime.tool_packs:
        config = load_config(env_prefix=runtime.spec.llm.env_prefix)
        selection = code_task_model_selection(config.routing)
        os.environ[model_env] = selection.model
        os.environ[effort_env] = selection.reasoning_effort
    else:
        os.environ.pop(model_env, None)
        os.environ.pop(effort_env, None)
    return run_service(argv)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
