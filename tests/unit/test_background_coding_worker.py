from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.application.agent_runtime import AgentRuntimeAssemblyProfile
from chatcopilot.botspec.runtime import BotPromptProfile
from chatcopilot.core.config import ChatConfig
from chatcopilot.middleware.runtime.jobs.worker import (
    _build_background_executor,
    run_worker,
)


def _request_payload(job_id: str) -> str:
    return (
        '{"job_id":"'
        + job_id
        + '","queue_name":"default","execution_policy":"background",'
        + '"tool_name":"external_diff","args":{},"workspace":{}}'
    )


def test_background_worker_rejects_symlinked_request_without_execution(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "jobs" / "job_symlinked_request"
    job_dir.mkdir(parents=True)
    private = tmp_path / "private-request.json"
    private.write_text(_request_payload(job_dir.name), encoding="utf-8")
    (job_dir / "request.json").symlink_to(private)

    with mock.patch(
        "chatcopilot.middleware.runtime.jobs.worker._build_background_executor"
    ) as build_executor:
        result = run_worker(job_dir / "request.json")

    assert result == 2
    build_executor.assert_not_called()
    assert not (job_dir / "result.json").exists()


def test_background_worker_rejects_symlinked_job_ancestor_without_execution(
    tmp_path: Path,
) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    external = tmp_path / "external-private-job"
    external.mkdir()
    request_path = external / "request.json"
    request_path.write_text(
        _request_payload("job_symlinked_ancestor"),
        encoding="utf-8",
    )
    job_dir = jobs_root / "job_symlinked_ancestor"
    job_dir.symlink_to(external, target_is_directory=True)

    with mock.patch(
        "chatcopilot.middleware.runtime.jobs.worker._build_background_executor"
    ) as build_executor:
        result = run_worker(job_dir / "request.json")

    assert result == 2
    build_executor.assert_not_called()
    assert not (external / "result.json").exists()


def test_generic_worker_uses_persisted_oversized_result_manifest_for_terminal_state(
    tmp_path: Path,
) -> None:
    job_id = "job_20260818_010000_deadbeef"
    job_dir = tmp_path / "jobs" / job_id
    job_dir.mkdir(parents=True, mode=0o700)
    request_path = job_dir / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "queue_name": "default",
                "execution_policy": "background",
                "tool_name": "external_diff",
                "args": {},
                "workspace": {},
                "attempts": [{"number": 1, "status": "running"}],
            }
        ),
        encoding="utf-8",
    )
    request_path.chmod(0o600)
    executor = mock.Mock()
    executor.execute.return_value = SimpleNamespace(
        ok=True,
        summary="x" * (8 * 1024 * 1024),
        outputs=[],
        console="",
        error=None,
        error_code="",
        details={},
        stage="",
    )

    with (
        mock.patch(
            "chatcopilot.middleware.runtime.jobs.worker._build_background_executor",
            return_value=(executor, None),
        ),
        mock.patch(
            "chatcopilot.middleware.runtime.jobs.worker.FileQueueSlot",
            return_value=mock.MagicMock(),
        ),
        # The production worker owns a short-lived process and intentionally
        # marks that process as a background worker.  Keep the in-process unit
        # fixture from leaking that process-scoped flag into later tests.
        mock.patch.dict(os.environ, {}, clear=False),
    ):
        exit_code = run_worker(request_path)

    assert exit_code == 1
    result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["stage"] == "failed"
    assert result["error_code"] == "result_artifact_too_large"
    assert status["status"] == "failed"
    assert status["stage"] == "failed"
    assert status["error_code"] == "result_artifact_too_large"
    assert request["attempts"][0]["status"] == "failed"
    assert request["attempts"][0]["error_code"] == "result_artifact_too_large"


class BackgroundCodingWorkerTests(unittest.TestCase):
    @mock.patch("chatcopilot.application.agent_runtime.assemble_agent_runtime")
    @mock.patch("chatcopilot.core.config.load_config")
    @mock.patch("chatcopilot.botspec.runtime.load_runtime_context")
    def test_coding_worker_rebuilds_dynamic_tools_with_runtime_env(
        self,
        load_runtime_context: mock.Mock,
        load_config: mock.Mock,
        assemble_agent_runtime: mock.Mock,
    ) -> None:
        executor = object()
        runtime = mock.Mock()
        runtime.new_session.return_value = SimpleNamespace(tool_executor=executor)
        assemble_agent_runtime.return_value = runtime
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
            agent_backend="native",
            prompt_profile=BotPromptProfile(
                identity="system",
                response_style="concise",
            ),
            capability_policies=(),
        )
        load_runtime_context.return_value = context
        load_config.return_value = ChatConfig()
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
        self.assertIs(assemble_agent_runtime.call_args.args[0], context)
        self.assertIs(
            assemble_agent_runtime.call_args.kwargs["profile"],
            AgentRuntimeAssemblyProfile.DETACHED,
        )
        runtime.new_session.assert_called_once_with(
            session_id="background-job-1",
            prompt_input=mock.ANY,
            workspace_service=workspace_service,
            caller_role_hint="owner",
        )
        built_prompt = runtime.new_session.call_args.kwargs["prompt_input"]
        self.assertEqual(built_prompt.profile.identity, "system")
        self.assertEqual(built_prompt.role, "owner")
        self.assertEqual(built_prompt.channel_kind, "private")

if __name__ == "__main__":
    unittest.main()
