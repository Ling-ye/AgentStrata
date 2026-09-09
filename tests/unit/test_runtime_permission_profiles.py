from pathlib import Path
import subprocess

import pytest

from chatcopilot.authorization.tools import ToolAuthorizationPolicy
from chatcopilot.contracts.authorization import (
    Principal,
    AuthorizationRequest,
    AuthorizationOperation,
)
from chatcopilot.contracts.identity import Role, ConversationIdentity
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.application.execution_scope import execution_scope
from chatcopilot.core.scoped_process import sandbox_command


def tool(access="owner"):
    return ToolDef(
        name="sample",
        summary="sample",
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=lambda a, c: ToolResult(ok=True),
        access=access,
        metadata={"private_chat_only": True, "execution_boundary": "codex"},
    )


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.USER])
@pytest.mark.parametrize("kind", ["p2p", "group"])
def test_profiles_are_independent_of_channel_and_old_metadata(role, kind):
    principal = Principal(
        channel="test",
        account_id="one",
        conversation=ConversationIdentity(platform="test", chat_kind=kind, chat_id="chat"),
        user_id="caller",
        role=role,
        evidence_digest="trusted",
    )
    request = AuthorizationRequest(
        request_id="r",
        principal=principal,
        operation=AuthorizationOperation.TOOL,
        target="sample",
        params_digest="args",
    )
    policy = ToolAuthorizationPolicy("v2")
    assert policy.decide(request, tool=tool()).allowed == (role is Role.OWNER)
    assert policy.decide(request, tool=tool("member")).allowed


def test_scoped_commands_can_write_project_but_cannot_reach_other_resources(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "other-instance"
    outside.mkdir()
    (outside / "secret").write_text("outside")
    scope = execution_scope(Role.OWNER, workspace, (project,))
    command = [
        "/bin/bash",
        "-c",
        f"printf result > output.txt; test ! -e {outside}/secret; test ! -w {outside}",
    ]
    result = subprocess.run(
        sandbox_command(command, scope=scope, cwd=project), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (project / "output.txt").read_text() == "result"
    assert (outside / "secret").read_text() == "outside"


def test_member_scope_has_no_project_or_native_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    scope = execution_scope(Role.USER, workspace, (project,))
    assert not scope.native_write and not scope.project_roots
    assert scope.permits(workspace / "file", write=True)
    assert not scope.permits(project / "file", write=True)


def test_actual_caller_role_reaches_handler_without_shared_mutation():
    from chatcopilot.agent.tools.executor import ToolExecutor

    observed = []
    item = tool("member")
    item.handler = lambda a, c: (observed.append(c.caller_role) or ToolResult(ok=True))
    executor = ToolExecutor(tools=[item])
    for role in (Role.OWNER, Role.ADMIN, Role.USER):
        assert executor.execute("sample", {}, role=role).ok
    assert observed == ["owner", "admin", "user"]


def test_mcp_catalog_receipt_is_separate_and_generation_bound():
    from chatcopilot.agent.backends.session_relay import SessionToolRelay, call_session_relay
    from chatcopilot.agent.backends.codex import CodexAgentBackend
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.contracts.agent import ToolCatalogObserved

    item = tool()
    relay = SessionToolRelay(tools=(item,), executor=ToolExecutor(tools=[item]))
    endpoint = relay.start()
    generation = relay.begin_turn(
        trace_id="run-a", parent_span_id="model-a", depth=0, request_text="request"
    )
    try:
        listed = call_session_relay(endpoint.to_dict(), {"action": "list_tools"})
        assert listed["generation"] == generation
        for current in (generation - 1, generation):
            call_session_relay(
                endpoint.to_dict(),
                {
                    "action": "catalog_observed",
                    "phase": "list_response_prepared",
                    "tools": ["sample"],
                    "generation": current,
                },
            )
        captured = []
        assert (
            CodexAgentBackend._emit_relay_tool_events(
                relay,
                captured.append,
                generation=generation,
                trace_id="run-a",
                parent_span_id="model-a",
            )
            == ""
        )
        assert len(captured) == 1 and isinstance(captured[0], ToolCatalogObserved)
        assert captured[0].tools == ("sample",) and captured[0].phase == "list_response_prepared"
    finally:
        relay.end_turn(generation)
        relay.close()


def test_migration_preview_leaves_input_untouched():
    from chatcopilot.botspec.permission_migration import preview_permission_migration

    raw = {
        "agents": {
            "backend": "codex",
            "codex": {"owner_access": "worktree", "member_access": "workspace"},
        },
        "context": {"dev": {"root_env": "PROJECT_ROOT", "allowed_paths": ["src/**"]}},
    }
    updated, removed = preview_permission_migration(raw)
    assert raw["agents"]["codex"]["owner_access"] == "worktree"
    assert updated == {
        "agents": {"backend": "codex"},
        "context": {"dev": {"root_env": "PROJECT_ROOT"}},
    }
    assert set(removed) == {
        "agents.codex.owner_access",
        "agents.codex.member_access",
        "context.dev.allowed_paths",
    }


def test_unbound_executor_does_not_grant_owner_by_default():
    from chatcopilot.agent.tools.executor import ToolExecutor

    called = []
    item = tool()
    item.handler = lambda a, c: (called.append(True) or ToolResult(ok=True))
    assert not ToolExecutor(tools=[item]).execute("sample", {}).ok
    assert not called


@pytest.mark.parametrize("role", [Role.OWNER, Role.USER])
def test_file_operations_reject_links_and_protected_state(tmp_path, role):
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.core.workspace_runtime import Workspace, MiddlewareWorkspaceService
    from chatcopilot.external_tools.dev.file_tools import TOOLS

    workspace = Workspace(
        root=tmp_path / "chat", user_id="actor", chat_kind="p2p", chat_id=None
    ).ensure()
    project = tmp_path / "project"
    project.mkdir()
    service = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        execution_scope=execution_scope(role, workspace.root, (project,)),
    )
    executor = ToolExecutor(tools=TOOLS, workspace_service=service, caller_role_hint=role.value)
    root = project if role is Role.OWNER else workspace.root
    secret = tmp_path / "other"
    secret.write_text("unchanged")
    (root / "symbolic").symlink_to(secret)
    import os

    os.link(secret, root / "linked")
    for path in ("symbolic", "linked", "../other", ".conversation-state/persona.md"):
        result = executor.execute("write_file", {"path": path, "content": "overwrite"})
        assert not result.ok, path
    assert not executor.execute("read_file", {"path": "symbolic"}).ok
    assert not executor.execute("read_file", {"path": "linked"}).ok
    assert executor.execute("write_file", {"path": "notes/new.txt", "content": "saved"}).ok
    assert (root / "notes/new.txt").read_text() == "saved"
    assert secret.read_text() == "unchanged"


def test_hidden_state_created_after_scope_is_never_exposed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scope = execution_scope(Role.OWNER, workspace)
    state = workspace / ".backend-sessions"
    state.mkdir()
    (state / "auth").write_text("private")
    assert not scope.permits(state / "auth")
    command = ["/bin/bash", "-c", "test ! -e .backend-sessions/auth && echo ordinary > file"]
    result = subprocess.run(
        sandbox_command(command, scope=scope, cwd=workspace), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (state / "auth").read_text() == "private"


@pytest.mark.parametrize("backend", ["native", "langgraph"])
@pytest.mark.parametrize("role", [Role.OWNER, Role.USER])
def test_agent_model_tool_flow_writes_only_bound_resources(tmp_path, backend, role):
    from tests.prompt_plan_fixture import prompt_input
    from chatcopilot.agent.runtime import AgentRuntime
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.llm_client import ChatResult
    from chatcopilot.core.workspace_runtime import Workspace, MiddlewareWorkspaceService
    from chatcopilot.external_tools.dev.file_tools import TOOLS
    from chatcopilot.contracts.agent import AgentTask

    workspace = Workspace(
        root=tmp_path / "chat", user_id="actor", chat_kind="p2p", chat_id=None
    ).ensure()
    project = tmp_path / "project"
    project.mkdir()
    scope = execution_scope(role, workspace.root, (project,))
    service = MiddlewareWorkspaceService(
        workspace=workspace, workspace_root=tmp_path, execution_scope=scope
    )

    class Model:
        model = "deterministic"

        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(
                    content="",
                    tool_calls=[
                        {
                            "id": "write-one",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": '{"path":"written.txt","content":"actual output"}',
                            },
                        }
                    ],
                )
            return ChatResult(content="completed")

        def close(self):
            pass

    model = Model()
    runtime = AgentRuntime(
        llm=model,
        tools=tuple(TOOLS),
        tools_schema=(),
        runtime_config=ChatConfig(),
        agent_backend=backend,
    )
    session = runtime.new_session(
        session_id="actor",
        prompt_input=prompt_input(role=role.value),
        workspace_service=service,
        caller_role_hint=role.value,
    )
    try:
        events = []
        result = session.run_task(AgentTask("write a test file"), on_event=events.append)
        assert result.final_text == "completed", (model.calls, result, events)
        root = project if role is Role.OWNER else workspace.root
        assert (root / "written.txt").read_text() == "actual output"
        assert not ((workspace.root if role is Role.OWNER else project) / "written.txt").exists()
        assert model.calls == 2
    finally:
        runtime.close()


@pytest.mark.parametrize("backend", ["native", "langgraph", "codex"])
@pytest.mark.parametrize("kind", ["group", "p2p"])
def test_persona_research_commits_and_next_turn_loads_in_each_backend(
    tmp_path, monkeypatch, backend, kind
):
    import json
    from tests.prompt_plan_fixture import prompt_input
    from tests.unit.test_persona_tools import _DraftAgent, _Port
    from chatcopilot.agent.persona import tools as persona_tools
    from chatcopilot.agent.runtime import AgentRuntime
    from chatcopilot.contracts.agent import AgentTask
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.llm_client import ChatResult
    from chatcopilot.core.workspace_runtime import Workspace, MiddlewareWorkspaceService
    from chatcopilot.agent.backends.session_relay import call_session_relay
    from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED

    request = (
        "设置此群人格，请自行搜索并生成完整的人格文件"
        if kind == "group"
        else "设置我的人格，请自行搜索并生成完整的人格文件"
    )
    workspace = Workspace(
        root=tmp_path / "ordinary",
        chat_kind=kind,
        chat_id="group" if kind == "group" else None,
        user_id="owner",
        scope=WORKSPACE_SCOPE_GROUP_SHARED if kind == "group" else "actor",
    ).ensure()
    service = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        platform_type="qq",
        backend_state_root=tmp_path / "backend",
        execution_scope=execution_scope(Role.OWNER, workspace.root),
    )
    monkeypatch.setattr(persona_tools, "PersonaDraftAgent", _DraftAgent)
    provider = persona_tools.build_persona_provider(
        _Port(chat_id=workspace.chat_id or ""), llm=object(), coordinator_factory=lambda: object()
    )
    params = {
        "operation": "research",
        "scope": "group" if kind == "group" else "user",
        "requirement": request,
    }

    class Model:
        model = "deterministic"

        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                assert any(t["function"]["name"] == "persona_manage" for t in kwargs["tools"])
                return ChatResult(
                    tool_calls=[
                        {
                            "id": "persona-one",
                            "type": "function",
                            "function": {"name": "persona_manage", "arguments": json.dumps(params)},
                        }
                    ]
                )
            return ChatResult(content="saved")

        def close(self):
            pass

    runtime = AgentRuntime(
        llm=Model(), tools=(), tools_schema=(), runtime_config=ChatConfig(), agent_backend=backend
    )
    session = runtime.new_session(
        session_id="owner",
        prompt_input=prompt_input(
            role="owner", channel_kind="group" if kind == "group" else "private"
        ),
        session_providers=(provider,),
        workspace_service=service,
        caller_role_hint="owner",
    )
    try:
        if backend == "codex":
            native = session.backend.native_session(session.backend_session_ref)
            generation = native.relay.begin_turn(
                trace_id="persona", parent_span_id="agent", depth=0, request_text=request
            )
            response = call_session_relay(
                json.loads(native.gateway_config.read_text())["relay"],
                {"action": "call_tool", "name": "persona_manage", "arguments": params},
            )
            native.relay.end_turn(generation)
            assert response["ok"] and response["result"]["data"]["committed"] is True
        else:
            events = []
            result = session.run_task(AgentTask(request), on_event=events.append)
            assert result.final_text == "saved"
            finish = next(e for e in events if type(e).__name__ == "ToolFinished")
            assert finish.data["data"]["committed"] is True
        reloaded = MiddlewareWorkspaceService(
            workspace=workspace, workspace_root=tmp_path, platform_type="qq"
        ).resolve_persistent_state()
        assert request in reloaded.persona_snapshot(params["scope"])
        assert not (workspace.root / "PERSONA.md").exists()
    finally:
        session.close()
        runtime.close()


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.USER])
def test_default_workspace_pack_can_write_ordinary_files_but_not_control_state(tmp_path, role):
    from chatcopilot.agent.tools.registry import discover_tools
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.core.workspace_runtime import Workspace, MiddlewareWorkspaceService

    workspace = Workspace(
        root=tmp_path / "chat", chat_kind="p2p", chat_id=None, user_id="actor"
    ).ensure()
    service = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        execution_scope=execution_scope(role, workspace.root),
    )
    executor = ToolExecutor(
        tools=discover_tools(tool_packs=["workspace.read_write"]),
        workspace_service=service,
        caller_role_hint=role.value,
    )
    assert executor.execute("write_workspace_file", {"path": "notes.txt", "content": "saved"}).ok
    assert (workspace.root / "notes.txt").read_text() == "saved"
    for path in ("tasks/forged.json", "../other", "PERSONA.md", ".conversation-state/memory.md"):
        assert not executor.execute("write_workspace_file", {"path": path, "content": "forged"}).ok
    assert executor.execute("write_workspace_file", {"path": "notes.txt", "operation": "delete"}).ok
    assert not (workspace.root / "notes.txt").exists()


def test_retired_model_role_config_reports_migration(tmp_path):
    import yaml
    from chatcopilot.botspec.loader import load_botspec

    data = yaml.safe_load(Path("bots/lingye-copilot-qq/bot.yaml").read_text())
    data["llm"]["code"]["allowed_roles"] = ["user"]
    path = tmp_path / "bot.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="llm.code.allowed_roles is retired"):
        load_botspec(path)


def test_member_cannot_schedule_hidden_lifecycle_tool(tmp_path):
    from tests.prompt_plan_fixture import prompt_plan
    from chatcopilot.agent.session import AgentSession
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.core.llm_client import ChatResult
    from chatcopilot.contracts.agent import AgentTask

    class Model:
        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            return (
                ChatResult(
                    content="request",
                    tool_calls=[
                        {
                            "id": "hidden",
                            "type": "function",
                            "function": {"name": "finalize_self_update", "arguments": "{}"},
                        }
                    ],
                )
                if self.calls == 1
                else ChatResult(content="done")
            )

    session = AgentSession(
        session_id="member",
        llm=Model(),
        executor=ToolExecutor(tools=[], caller_role_hint="user"),
        tools_schema=[],
        prompt_plan=prompt_plan(role="user"),
    )
    result = session.run_task(AgentTask("ordinary task"), on_event=lambda e: None)
    assert result.lifecycle_intents == ()


@pytest.mark.parametrize("role", ["owner", "user", "admin"])
def test_unbound_file_call_cannot_fall_back_to_process_project(tmp_path, monkeypatch, role):
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.external_tools.dev.file_tools import TOOLS
    from chatcopilot.external_tools.dev.config import reset_cache

    monkeypatch.setenv("CHATCOPILOT_DEV_ROOT", str(tmp_path))
    reset_cache()
    try:
        result = ToolExecutor(tools=TOOLS, caller_role_hint=role).execute(
            "write_file", {"path": "must-not-exist", "content": "outside"}
        )
        assert not result.ok and "execution resources are not bound" in result.error
        assert not (tmp_path / "must-not-exist").exists()
        from chatcopilot.agent.tools.builtin.workspace_tools import TOOLS as workspace_tools
        result = ToolExecutor(tools=workspace_tools, caller_role_hint=role).execute("write_workspace_file", {"path":"must-not-exist","content":"outside"})
        assert not result.ok and "execution resources are not bound" in result.error
    finally:
        reset_cache()
