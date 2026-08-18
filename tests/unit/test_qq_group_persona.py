from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chatcopilot.agent.tools.builtin import persona_tools
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import AccessSpec
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.agent_bridge import (
    _extract_persona_snippet,
    _make_permission_filter,
)
from chatcopilot.middleware.acp.project_access import restricted_project_request_reply
from chatcopilot.middleware.runtime.workspace import (
    MiddlewareWorkspaceService,
    Workspace,
)


def _workspace(tmp_path: Path, *, actor_id: str = "10001") -> Workspace:
    return Workspace(
        root=tmp_path / "group_30003" / "shared",
        chat_kind="group",
        chat_id="30003",
        user_id=actor_id,
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
    ).ensure()


def _executor(tmp_path: Path, *, role: Role, actor_id: str) -> ToolExecutor:
    workspace = _workspace(tmp_path, actor_id=actor_id)
    return ToolExecutor(
        tools=list(persona_tools.TOOLS),
        workspace_service=MiddlewareWorkspaceService(
            workspace=workspace,
            workspace_root=tmp_path,
        ),
        caller_role_hint=role.value,
        permission_filter=_make_permission_filter(
            role,
            workspace,
            owner_only_project_access=False,
            agent_backend="codex",
        ),
    )


def _session(tmp_path: Path, role: Role) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        workspace=_workspace(tmp_path, actor_id=role.value),
        runtime=SimpleNamespace(
            access=AccessSpec(owner_only_project_access=True)
        ),
    )


def test_owner_group_reuses_the_generic_persona_tools(tmp_path: Path) -> None:
    executor = _executor(tmp_path, role=Role.OWNER, actor_id="10001")

    result = executor.execute(
        "persona_set",
        {"text": "克制、疏离，使用略带情绪的原创短句。"},
    )

    assert result.ok is True
    group_persona = tmp_path / "group_30003" / "PERSONA.md"
    assert group_persona.read_text(encoding="utf-8") == (
        "克制、疏离，使用略带情绪的原创短句。\n"
    )
    assert not (
        tmp_path
        / "group_30003"
        / ".conversation-state"
        / "GROUP_PERSONA.md"
    ).exists()
    assert {tool.name for tool in persona_tools.TOOLS} == {
        "persona_show",
        "persona_set",
        "persona_append",
        "persona_clear",
    }


def test_non_owner_group_cannot_mutate_shared_persona(tmp_path: Path) -> None:
    executor = _executor(tmp_path, role=Role.USER, actor_id="20002")

    result = executor.execute("persona_set", {"text": "不应写入"})

    assert result.ok is False
    assert "Owner" in (result.error or "")
    assert not (tmp_path / "group_30003" / "PERSONA.md").exists()


def test_group_persona_is_shared_prompt_context_without_private_layers(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    tmp_path.joinpath("PERSONA.md").write_text(
        "private global persona", encoding="utf-8"
    )
    workspace.root.parent.joinpath("PERSONA.md").write_text(
        "current group persona", encoding="utf-8"
    )
    workspace.root.joinpath("PERSONA.md").write_text(
        "untrusted shared file", encoding="utf-8"
    )
    runtime = SimpleNamespace(access=AccessSpec(owner_only_project_access=True))

    for role in (Role.OWNER, Role.USER):
        prompt = _extract_persona_snippet(runtime, role, workspace)
        assert "current group persona" in prompt
        assert "private global persona" not in prompt
        assert "untrusted shared file" not in prompt


def test_owner_group_persona_request_uses_normal_role_authorization(
    tmp_path: Path,
) -> None:
    text = "模仿下某个角色的性格和说话风格，用作你在此群未来的人设"

    assert restricted_project_request_reply(_session(tmp_path, Role.OWNER), text) is None
    assert restricted_project_request_reply(_session(tmp_path, Role.USER), text) is None
