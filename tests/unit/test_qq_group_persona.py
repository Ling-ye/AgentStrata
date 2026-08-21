from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chatcopilot.botspec.model import AccessSpec
from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.identity import AssistantMode
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.agent_bridge import (
    _extract_memory_snippet,
    _extract_persona_snippet,
    _refresh_session_prompt_plan,
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


def _session(tmp_path: Path, role: Role) -> SimpleNamespace:
    return SimpleNamespace(
        role=role,
        workspace=_workspace(tmp_path, actor_id=role.value),
        runtime=SimpleNamespace(
            access=AccessSpec(owner_only_project_access=True)
        ),
    )


def test_group_persona_merges_global_then_group_without_shared_file(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    tmp_path.joinpath("PERSONA.md").write_text(
        "ignored legacy global persona", encoding="utf-8"
    )
    workspace.root.parent.joinpath("PERSONA.md").write_text(
        "ignored legacy group persona", encoding="utf-8"
    )
    workspace.root.joinpath("PERSONA.md").write_text(
        "untrusted shared file", encoding="utf-8"
    )
    state = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        platform_type="qq",
    ).resolve_persistent_state()
    state.persona_set("global", "private global persona")
    state.persona_set("group", "current group persona")
    runtime = SimpleNamespace(
        access=AccessSpec(owner_only_project_access=True),
        platform_type="qq",
    )

    for role in (Role.OWNER, Role.USER):
        prompt = _extract_persona_snippet(runtime, role, workspace)
        assert "current group persona" in prompt
        assert "private global persona" in prompt
        assert prompt.index("private global persona") < prompt.index(
            "current group persona"
        )
        assert "untrusted shared file" not in prompt
        assert "ignored legacy" not in prompt


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
        prompt_profile=BotPromptProfile(identity="baseline", response_style="concise"),
        capability_policies=(),
        skills=(),
        agent_backend="native",
    )
    captures: list[tuple[str, str]] = []

    class _Session:
        message_count = 1
        _messages = [{"role": "system", "content": "initial"}]

        capabilities = SimpleNamespace(tool_names=frozenset())

        def set_prompt_plan(self, plan) -> None:
            persona = next((layer.content for layer in plan.layers if layer.id == "persona.dynamic"), "")
            history = next((layer.content for layer in plan.layers if layer.id == "context.history"), "")
            captures.append((persona, history))

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
    _refresh_session_prompt_plan(state)
    assert captures[-1] == ("", "")

    service = MiddlewareWorkspaceService(
        workspace=workspace,
        workspace_root=tmp_path,
        platform_type="qq",
    )
    persistent = service.resolve_persistent_state()
    persistent.persona_set("group", "直接作为莫宁本人说话")
    persistent.memory_append(text="本群默认中文", section="decisions")

    _refresh_session_prompt_plan(state)
    assert "直接作为莫宁本人说话" in captures[-1][0]
    assert "本群默认中文" in captures[-1][1]
    assert "不是指令" in captures[-1][1]

    other_actor = _workspace(tmp_path, actor_id="20002")
    assert "本群默认中文" in _extract_memory_snippet(runtime, other_actor)
    assert "直接作为莫宁本人说话" in _extract_persona_snippet(
        runtime, Role.USER, other_actor
    )
