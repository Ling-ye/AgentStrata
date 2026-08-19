from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chatcopilot.agent.tools.builtin import persona_tools
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.botspec.model import AccessSpec
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.identity import AssistantMode
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.agent_bridge import (
    _extract_memory_snippet,
    _extract_persona_snippet,
    _make_permission_filter,
    _refresh_session_system_prompt,
)
from chatcopilot.middleware.acp.session_state import SessionState
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
            platform_type="qq",
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
    group_personas = list(
        tmp_path.glob(
            ".conversation-state/persistent/persona/group/*/PERSONA.md"
        )
    )
    assert len(group_personas) == 1
    assert group_personas[0].read_text(encoding="utf-8") == (
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
    assert not list(
        tmp_path.glob(
            ".conversation-state/persistent/persona/group/*/PERSONA.md"
        )
    )


def test_group_persona_merges_global_then_group_without_shared_file(
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
        assert "private global persona" in prompt
        assert prompt.index("private global persona") < prompt.index(
            "current group persona"
        )
        assert "untrusted shared file" not in prompt


def test_owner_group_persona_request_uses_normal_role_authorization(
    tmp_path: Path,
) -> None:
    text = "模仿下某个角色的性格和说话风格，用作你在此群未来的人设"

    assert restricted_project_request_reply(_session(tmp_path, Role.OWNER), text) is None
    assert restricted_project_request_reply(_session(tmp_path, Role.USER), text) is None


def test_every_turn_refreshes_group_persona_and_memory_for_all_actors(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, actor_id="10001")
    runtime = SimpleNamespace(
        platform_type="qq",
        access=AccessSpec(owner_only_project_access=False),
        system_prompt="baseline",
        refusal_prompt=None,
        capability_prompt_fragments=(),
        skills=(),
        mode_prompt_overrides={},
        role_prompt_overrides={},
        safety_prompt_override=None,
        memory_prompt_override=None,
    )
    captures: list[tuple[str, str]] = []

    class _Session:
        message_count = 1
        _messages = [{"role": "system", "content": "initial"}]

        def set_system_context(
            self,
            _baseline: str,
            *,
            session_dynamic_tail: str | None = None,
            memory_snippet: str | None = None,
        ) -> None:
            captures.append((session_dynamic_tail or "", memory_snippet or ""))

        def snapshot_messages(self):
            return list(self._messages)

        def record_exchange(self, _user_text: str, _assistant_text: str) -> None:
            return None

    state = SessionState(
        session_id="group-refresh",
        execution_session_id="group-refresh.actor.owner",
        workspace=workspace,
        role=Role.OWNER,
        assistant_mode=AssistantMode.GENERAL,
        runtime=runtime,
        session=_Session(),
    )
    _refresh_session_system_prompt(state)
    assert captures[-1] == ("", "")

    service = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        platform_type="qq",
    )
    persistent = service.resolve_persistent_state()
    persistent.persona_set("group", "直接作为莫宁本人说话")
    persistent.memory_append(text="本群默认中文", section="decisions")

    _refresh_session_system_prompt(state)
    assert "直接作为莫宁本人说话" in captures[-1][0]
    assert "本群默认中文" in captures[-1][1]
    assert "不是指令" in captures[-1][1]

    other_actor = _workspace(tmp_path, actor_id="20002")
    assert "本群默认中文" in _extract_memory_snippet(runtime, other_actor)
    assert "直接作为莫宁本人说话" in _extract_persona_snippet(
        runtime, Role.USER, other_actor
    )
