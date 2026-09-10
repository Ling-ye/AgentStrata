from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from chatcopilot.application.agent_runtime import project_agent_runtime
from chatcopilot.application.execution_scope import execution_scope
from chatcopilot.contracts.execution_scope import CommandTimeouts, bind_execution_scope
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.tools import EXECUTION_GLOBAL_SERIAL_BACKGROUND, ToolContext, ToolResult
from chatcopilot.core.caller_context import bind_caller_role
from chatcopilot.core.config import ChatConfig, load_command_timeouts
from chatcopilot.core.jobs import write_json_atomic
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.external_tools.dev.config import get_dev_config
from chatcopilot.external_tools.dev import shell_tools
from chatcopilot.middleware.runtime.jobs import submitter, worker
from tests.unit.test_application_agent_runtime import _runtime


def _projection(*, default=60, maximum=300, environment=None):
    runtime = _runtime()
    dev = runtime.spec.context.dev
    runtime.spec.context = replace(runtime.spec.context, dev=replace(
        dev, shell=replace(dev.shell, timeout_default=default, timeout_max=maximum),
    ))
    return project_agent_runtime(
        runtime, chat_config=ChatConfig(),
        environment={} if environment is None else environment,
    )


@pytest.mark.parametrize("default, maximum, override, expected", [
    (60, 300, None, CommandTimeouts()),
    (90, 600, None, CommandTimeouts(90, 600)),
    (90, 600, "1200", CommandTimeouts(90, 1200)),
    (90, 600, "30", CommandTimeouts(30, 30)),
    (90, 600, "", CommandTimeouts(90, 600)),
])
def test_projection_preserves_declared_timeouts_and_environment_precedence(
    default, maximum, override, expected,
):
    environment = {} if override is None else {"CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX": override}
    assert _projection(default=default, maximum=maximum, environment=environment).command_timeouts == expected


@pytest.mark.parametrize("value", ["0", "-1", "invalid", "1.5"])
def test_invalid_explicit_environment_timeout_is_rejected(value):
    with pytest.raises(ValueError, match="positive integer"):
        _projection(environment={"CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX": value})


@pytest.mark.parametrize("field, value", [
    ("timeout_default", 0), ("timeout_default", True), ("timeout_default", 1.5),
    ("timeout_max", -1), ("timeout_max", True), ("timeout_max", "1200"),
])
def test_command_timeouts_require_positive_integers(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        CommandTimeouts(**{field: value})


def test_bound_instances_keep_separate_frozen_timeouts_after_environment_changes(tmp_path, monkeypatch):
    environment = {"CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX": "1200"}
    first = _projection(default=80, environment=environment)
    environment["CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX"] = "40"
    second = _projection(default=20, environment=environment)
    monkeypatch.setenv("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX", "invalid-after-start")
    for projection, expected in ((first, CommandTimeouts(80, 1200)), (second, CommandTimeouts(20, 40)), (first, CommandTimeouts(80, 1200))):
        scope = execution_scope(Role.OWNER, tmp_path, command_timeouts=projection.command_timeouts)
        with bind_execution_scope(scope):
            actual = get_dev_config(force_reload=True)
            assert (actual.shell.timeout_default, actual.shell.timeout_max) == (expected.timeout_default, expected.timeout_max)
        with pytest.raises(FrozenInstanceError):
            projection.command_timeouts.timeout_max = 1


@pytest.mark.parametrize("requested, expected", [(None, 45), (800, 800), (2000, 1200), (0, 1)])
def test_run_command_applies_frozen_budget_and_clamps_request(tmp_path, monkeypatch, requested, expected):
    scope = execution_scope(Role.OWNER, tmp_path, command_timeouts=CommandTimeouts(45, 1200))
    captured = []
    monkeypatch.setenv("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX", "2")

    def run(_command, **kwargs):
        captured.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="probe", stderr="")

    monkeypatch.setattr(shell_tools.subprocess, "run", run)
    arguments = {"command": "echo probe"}
    if requested is not None:
        arguments["timeout_seconds"] = requested
    with bind_execution_scope(scope):
        result = shell_tools._handle_run_command(arguments, ToolContext(execution_scope=scope))
    assert result.ok and captured == [expected]


def test_gateway_actor_binds_runtime_command_timeouts(tmp_path):
    from chatcopilot.application.actor_runtime import ActorTurnExecutor
    from tests.unit.test_application_actor_runtime import _factory, _principal, _turn

    factory, runtime, _ = _factory(tmp_path)
    runtime.command_timeouts = CommandTimeouts(90, 1200)
    executor = ActorTurnExecutor(factory)
    try:
        asyncio.run(executor.execute(_turn(
            session_id="session-1", principal=_principal("20002", role=Role.OWNER),
            canonical_text="fixture", message_id="command-budget",
        ), on_event=lambda _event: None))
        scope = runtime.creations[0]["workspace_service"].execution_scope
        assert scope.command_timeouts is runtime.command_timeouts
    finally:
        executor.close()
        factory.close()


@pytest.fixture
def background_request(tmp_path, monkeypatch):
    workspace = Workspace(root=tmp_path / "chat", chat_kind="p2p", chat_id=None, user_id="owner").ensure()
    monkeypatch.setenv("CHATCOPILOT_LIMIT_DIR", str(tmp_path / "limits"))
    monkeypatch.setattr(submitter, "_spawn_worker", lambda *_args: None)
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    scope = execution_scope(Role.OWNER, workspace.root, readonly_roots=(readonly,), command_timeouts=CommandTimeouts(45, 1200))
    with bind_execution_scope(scope), bind_caller_role("owner"):
        job = submitter.submit_tool_job(
            tool_name="fixture", args={}, execution_policy=EXECUTION_GLOBAL_SERIAL_BACKGROUND,
            workspace=workspace,
        )
    return job.request_path


@pytest.mark.parametrize("legacy", [False, True])
def test_background_request_carries_budget_and_worker_ignores_later_environment(
    background_request, monkeypatch, legacy,
):
    request = json.loads(background_request.read_text())
    assert request["command_timeouts"] == {"timeout_default": 45, "timeout_max": 1200}
    assert len(request["readonly_roots"]) == 1
    if legacy:
        request.pop("command_timeouts")
        write_json_atomic(background_request, request)
    observed = []

    def build(**kwargs):
        scope = kwargs["workspace_service"].execution_scope
        observed.append(scope.command_timeouts)
        readonly = Path(request["readonly_roots"][0])
        assert scope.permits(readonly) and not scope.permits(readonly, write=True)
        return SimpleNamespace(execute=lambda *_args: ToolResult(ok=True, summary="fixture")), None

    monkeypatch.setattr(worker, "_build_background_executor", build)
    with mock.patch.dict(os.environ, {"CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX": "9000"}):
        assert worker.run_worker(background_request) == 0
    assert observed == [CommandTimeouts() if legacy else CommandTimeouts(45, 1200)]


@pytest.mark.parametrize("snapshot", [None, {}, {"timeout_max": 1200}, {"timeout_default": 60, "timeout_max": False}])
def test_background_worker_rejects_malformed_new_snapshot_before_execution(background_request, monkeypatch, snapshot):
    request = json.loads(background_request.read_text())
    request["command_timeouts"] = snapshot
    write_json_atomic(background_request, request)
    build = mock.Mock()
    monkeypatch.setattr(worker, "_build_background_executor", build)
    assert worker.run_worker(background_request) == 2
    build.assert_not_called()


def test_standalone_mcp_binds_startup_environment_timeouts(tmp_path, monkeypatch):
    import mcp.server
    import mcp.server.stdio
    from chatcopilot.middleware.mcp import server as endpoint

    workspace = Workspace(root=tmp_path / "chat", chat_kind="p2p", chat_id=None, user_id="owner").ensure()
    captured = {}
    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(tmp_path))
    monkeypatch.setenv("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX", "1200")
    monkeypatch.setattr(endpoint, "resolve_workspace", lambda **_kwargs: workspace)
    monkeypatch.setattr(endpoint, "build_mcp_tools_schema", lambda: ([], {}))
    monkeypatch.setattr(endpoint, "cleanup_workspace", lambda _workspace: {})

    def executor(**kwargs):
        captured["scope"] = kwargs["workspace_service"].execution_scope
        return SimpleNamespace(execute=lambda *_args: ToolResult(ok=True))

    class Server:
        def __init__(self, _name):
            pass

        def list_tools(self):
            return lambda handler: handler

        call_tool = list_tools

        def create_initialization_options(self):
            return None

        async def run(self, *_args):
            monkeypatch.setenv("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX", "1")

    @asynccontextmanager
    async def stdio():
        yield None, None

    monkeypatch.setattr(endpoint, "ToolExecutor", executor)
    monkeypatch.setattr(mcp.server, "Server", Server)
    monkeypatch.setattr(mcp.server.stdio, "stdio_server", stdio)
    assert asyncio.run(endpoint._run_stdio_server()) == 0
    with bind_execution_scope(captured["scope"]):
        assert get_dev_config().shell.timeout_max == 1200


def test_unbound_host_timeout_resolution_uses_same_positive_budget_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(tmp_path))
    monkeypatch.setenv("CHATCOPILOT_DEV_SHELL_TIMEOUT_MAX", "15")
    with bind_execution_scope(None):
        config = get_dev_config(force_reload=True)
    assert (config.shell.timeout_default, config.shell.timeout_max) == (15, 15)
    assert load_command_timeouts(environment={}) == CommandTimeouts()
