from __future__ import annotations

import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.middleware.runtime.jobs.worker import _build_background_executor


class BackgroundCodingWorkerTests(unittest.TestCase):
    @mock.patch("chatcopilot.agent.runtime.build_agent_runtime")
    @mock.patch("chatcopilot.core.config.load_config")
    @mock.patch("chatcopilot.botspec.runtime.load_runtime_context")
    def test_coding_worker_rebuilds_dynamic_tools_with_runtime_env(
        self,
        load_runtime_context: mock.Mock,
        load_config: mock.Mock,
        build_agent_runtime: mock.Mock,
    ) -> None:
        executor = object()
        runtime = mock.Mock()
        runtime.new_session.return_value = SimpleNamespace(executor=executor)
        build_agent_runtime.return_value = runtime
        spec = SimpleNamespace(
            llm=SimpleNamespace(env_prefix="CHATCOPILOT_TEST"),
            context=SimpleNamespace(
                codebases=SimpleNamespace(registry="codebases/repositories.yaml"),
                dev=SimpleNamespace(
                    root_env="CHATCOPILOT_DEV_ROOT",
                    allowed_paths=(),
                    denied_paths=(),
                    shell=SimpleNamespace(timeout_max=300),
                ),
                wiki=SimpleNamespace(
                    enabled=False,
                    root_env="CHATCOPILOT_WIKI_ROOT",
                    max_chunk_chars=1200,
                ),
            ),
            resolve_path=lambda value: Path("/tmp/bot") / str(value) if value else None,
        )
        mcp_servers = (object(),)
        context = SimpleNamespace(
            spec=spec,
            bot_id="test-bot",
            instance_id="test-bot",
            display_name="Test Bot",
            workspace_root="/tmp/workspace",
            log_dir="/tmp/logs",
            source_path=Path.cwd() / "bots" / "lingye-copilot-qq" / "bot.yaml",
            tool_packs=("codebase.read",),
            tool_features=(),
            exclude_tools=(),
            skills=(),
            rag_sources=(),
            mcp_servers=mcp_servers,
            subagents=SimpleNamespace(include=("code_implementer", "code_publisher")),
            system_prompt="system",
        )
        load_runtime_context.return_value = context
        load_config.return_value = object()
        workspace_service = object()

        with mock.patch.dict(os.environ, {}, clear=False):
            actual, actual_runtime = _build_background_executor(
                tool_name="run_coding_workflow",
                job_id="job-1",
                workspace_service=workspace_service,
            )

        self.assertIs(actual, executor)
        self.assertIs(actual_runtime, runtime)
        load_config.assert_called_once_with(env_prefix="CHATCOPILOT_TEST")
        self.assertEqual(build_agent_runtime.call_args.kwargs["mcp_servers"], mcp_servers)
        runtime.new_session.assert_called_once_with(
            session_id="background-job-1",
            system_baseline="system",
            workspace_service=workspace_service,
            caller_role_hint="owner",
        )

if __name__ == "__main__":
    unittest.main()
