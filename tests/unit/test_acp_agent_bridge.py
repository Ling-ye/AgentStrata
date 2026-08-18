from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.botspec.model import (
    AccessSpec,
    BotSpec,
    ContextSpec,
    PlatformSpec,
    PromptSpec,
    WikiSpec,
)
from chatcopilot.contracts import Role
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.agent_bridge import (
    _authorized_wiki_retriever,
    _build_session_for_workspace,
    _effective_project_role,
    _extract_persona_snippet,
    _make_permission_filter,
    _materialize_session_for_workspace,
    _prompt_projection,
)
from chatcopilot.middleware.runtime.workspace import Workspace


def _tool(
    *,
    category: str = "wiki.knowledge",
    requires_role: str | None = "owner",
    metadata: dict | None = None,
) -> ToolDef:
    return ToolDef(
        name="private_wiki_tool",
        summary="test",
        properties={},
        required=[],
        handler=lambda args: ("ok", [], None),
        requires_role=requires_role,
        category=category,
        metadata=(
            metadata if metadata is not None else {"private_chat_only": True}
        ),
    )


def _workspace(tmp_path: Path, kind: str) -> Workspace:
    if kind == "group":
        return Workspace(
            root=tmp_path / "group_chat-1" / "shared",
            chat_kind="group",
            chat_id="chat-1",
            user_id="owner-1",
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        ).ensure()
    return Workspace(
        root=tmp_path / kind,
        chat_kind=kind,
        chat_id="chat-1",
        user_id="owner-1",
    ).ensure()


def _runtime(tmp_path: Path):
    spec = BotSpec(
        id="wiki-bot",
        display_name="Wiki Bot",
        source_path=tmp_path / "bot.yaml",
        platform=PlatformSpec(type="qq", adapter="qq_acp"),
        prompts=PromptSpec(persona="persona.md"),
        context=ContextSpec(
            wiki=WikiSpec(enabled=True, read_role="owner", private_chat_only=True)
        ),
    )
    return SimpleNamespace(spec=spec)


def test_permission_filter_requires_owner_and_private_chat(tmp_path: Path) -> None:
    private_ws = _workspace(tmp_path, "p2p")
    group_ws = _workspace(tmp_path, "group")

    assert _make_permission_filter(Role.OWNER, private_ws)(_tool()) is None
    assert "仅允许在私聊" in str(_make_permission_filter(Role.OWNER, group_ws)(_tool()))
    assert "需要 owner" in str(_make_permission_filter(Role.USER, private_ws)(_tool()))


def test_owner_only_project_filter_preserves_owner_role_in_group_chat(
    tmp_path: Path,
) -> None:
    owner_private = _workspace(tmp_path, "p2p")
    owner_group = _workspace(tmp_path, "group")
    user_filter = _make_permission_filter(
        Role.USER,
        owner_private,
        owner_only_project_access=True,
    )
    owner_private_filter = _make_permission_filter(
        Role.OWNER,
        owner_private,
        owner_only_project_access=True,
    )
    owner_group_filter = _make_permission_filter(
        Role.OWNER,
        owner_group,
        owner_only_project_access=True,
    )
    group_user_filter = _make_permission_filter(
        Role.USER,
        owner_group,
        owner_only_project_access=False,
    )

    safe = _tool(
        category="agent.search",
        requires_role=None,
        metadata={},
    )
    host = _tool(
        category="filesystem.windows.read",
        requires_role=None,
        metadata={},
    )
    unknown = _tool(category="new.unclassified", requires_role=None, metadata={})
    mcp_search = _tool(
        category="mcp",
        requires_role=None,
        metadata={"mcp_risk": "search"},
    )
    mcp_readonly = _tool(
        category="mcp",
        requires_role=None,
        metadata={"mcp_risk": "readonly"},
    )

    assert user_filter(safe) is None
    assert user_filter(mcp_search) is None
    assert "仅限 Owner" in str(user_filter(host))
    assert "仅限 Owner" in str(user_filter(unknown))
    assert "仅限 Owner" in str(user_filter(mcp_readonly))
    assert owner_private_filter(host) is None
    assert owner_private_filter(unknown) is None
    assert owner_group_filter(host) is None
    assert owner_group_filter(unknown) is None
    assert "仅限 Owner" in str(group_user_filter(host))
    assert "仅限 Owner" in str(group_user_filter(unknown))


def test_restricted_prompt_projection_preserves_owner_role_in_group_chat(
    tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(
        access=AccessSpec(owner_only_project_access=True),
        capability_prompt_fragments=("internal capability",),
        skills=("internal skill",),
    )

    private_ws = _workspace(tmp_path, "p2p")
    group_ws = _workspace(tmp_path, "group")

    assert _prompt_projection(runtime, Role.USER, private_ws) == ((), ())
    assert _prompt_projection(runtime, Role.ADMIN, private_ws) == ((), ())
    assert _prompt_projection(runtime, Role.OWNER, group_ws) == (
        ("internal capability",),
        ("internal skill",),
    )
    assert _prompt_projection(runtime, Role.OWNER, private_ws) == (
        ("internal capability",),
        ("internal skill",),
    )
    assert _effective_project_role(runtime, Role.OWNER, private_ws) == Role.OWNER
    assert _effective_project_role(runtime, Role.OWNER, group_ws) == Role.OWNER


def test_shared_group_persona_projection_exposes_only_group_layer(
    tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(access=AccessSpec(owner_only_project_access=True))
    group_ws = Workspace(
        root=tmp_path / "group_chat-1" / "shared",
        chat_kind="group",
        chat_id="chat-1",
        user_id="owner-1",
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
    ).ensure()
    workspace_root = tmp_path
    workspace_root.joinpath("PERSONA.md").write_text(
        "private global persona", encoding="utf-8"
    )
    group_ws.root.parent.joinpath("PERSONA.md").write_text(
        "current group persona", encoding="utf-8"
    )
    group_ws.root.joinpath("PERSONA.md").write_text(
        "untrusted shared file", encoding="utf-8"
    )

    user_prompt = _extract_persona_snippet(runtime, Role.USER, group_ws)
    owner_group_prompt = _extract_persona_snippet(runtime, Role.OWNER, group_ws)

    for prompt in (user_prompt, owner_group_prompt):
        assert "current group persona" in prompt
        assert "private global persona" not in prompt
        assert "untrusted shared file" not in prompt


def test_restricted_persona_projection_allows_owner_private_layers(
    tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(access=AccessSpec(owner_only_project_access=True))
    private_ws = Workspace(
        root=tmp_path / "p2p_owner-1",
        chat_kind="p2p",
        chat_id="owner-1",
        user_id="owner-1",
    ).ensure()
    workspace_root = private_ws.root.parent
    workspace_root.joinpath("PERSONA.md").write_text(
        "private global persona", encoding="utf-8"
    )
    private_ws.root.joinpath("PERSONA.md").write_text(
        "owner preference", encoding="utf-8"
    )

    prompt = _extract_persona_snippet(runtime, Role.OWNER, private_ws)

    assert "private global persona" in prompt
    assert "owner preference" in prompt


def test_wiki_retriever_is_only_created_for_owner_private_session(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    root = tmp_path / "wiki"

    with mock.patch.dict(os.environ, {"CHATCOPILOT_WIKI_ROOT": str(root)}, clear=True):
        owner_private = _authorized_wiki_retriever(
            runtime=runtime, role=Role.OWNER, ws=_workspace(tmp_path, "p2p")
        )
        owner_group = _authorized_wiki_retriever(
            runtime=runtime, role=Role.OWNER, ws=_workspace(tmp_path, "group")
        )
        user_private = _authorized_wiki_retriever(
            runtime=runtime, role=Role.USER, ws=_workspace(tmp_path, "p2p-user")
        )

    assert owner_private is not None
    assert owner_group is None
    assert user_private is None


def test_control_session_materialization_replays_buffered_exchange(tmp_path: Path) -> None:
    spec = BotSpec(
        id="lazy-bot",
        display_name="Lazy Bot",
        source_path=tmp_path / "bot.yaml",
        platform=PlatformSpec(type="qq", adapter="qq_acp"),
        prompts=PromptSpec(persona="persona.md"),
        context=ContextSpec(wiki=WikiSpec(enabled=False)),
    )
    runtime = SimpleNamespace(
        spec=spec,
        platform_type="qq",
        system_prompt="system",
        refusal_prompt=None,
        capability_prompt_fragments=(),
        skills=(),
        mode_prompt_overrides={},
        role_prompt_overrides={},
        safety_prompt_override=None,
        memory_prompt_override=None,
    )
    workspace = replace(_workspace(tmp_path, "p2p"), user_name="Example User")
    state = _build_session_for_workspace(
        session_id="sid-lazy-bridge",
        ws=workspace,
        agent_runtime=None,
        runtime=runtime,
        llm_model="test-model",
    )
    state.record_exchange("job status?", "delegated")

    messages: list[tuple[str, str]] = []
    agent_session = SimpleNamespace(
        record_exchange=lambda user, assistant: messages.append((user, assistant)),
        snapshot_messages=lambda: [
            {"role": role, "content": text}
            for user, assistant in messages
            for role, text in (("user", user), ("assistant", assistant))
        ],
        message_count=2,
        _messages=[],
        set_system_baseline=lambda _value: None,
    )
    agent_runtime = SimpleNamespace(
        retriever=None,
        new_session=mock.Mock(return_value=agent_session),
    )

    _materialize_session_for_workspace(state, agent_runtime=agent_runtime)

    assert state.is_materialized
    assert state.role == Role.USER
    assert messages == [("job status?", "delegated")]
    agent_runtime.new_session.assert_called_once()
    open_kwargs = agent_runtime.new_session.call_args.kwargs
    assert open_kwargs["caller_role_hint"] == "user"
    assert open_kwargs["caller_identity"].user_id == "owner-1"
    assert open_kwargs["caller_identity"].user_name == "Example User"
