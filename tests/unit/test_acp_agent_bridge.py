from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.botspec.model import BotSpec, ContextSpec, PlatformSpec, PromptSpec, WikiSpec
from chatcopilot.contracts import Role
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.middleware.acp.agent_bridge import (
    _authorized_wiki_retriever,
    _build_session_for_workspace,
    _make_permission_filter,
    _materialize_session_for_workspace,
)
from chatcopilot.middleware.runtime.workspace import Workspace


def _tool() -> ToolDef:
    return ToolDef(
        name="private_wiki_tool",
        summary="test",
        properties={},
        required=[],
        handler=lambda args: ("ok", [], None),
        requires_role="owner",
        metadata={"private_chat_only": True},
    )


def _workspace(tmp_path: Path, kind: str) -> Workspace:
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
