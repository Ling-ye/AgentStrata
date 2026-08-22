from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatcopilot.contracts import AssistantMode, Role
from chatcopilot.contracts.identity import ConversationIdentity, TurnIdentity
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.group_conversation import (
    GroupConversationJournal,
    SenderEnvelopeError,
)
from chatcopilot.middleware.acp.server import AcpChatAgent
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.core.workspace_runtime import Workspace


class _DiscardableBackend:
    def __init__(self) -> None:
        self.discard_count = 0

    def discard(self) -> None:
        self.discard_count += 1


def test_journal_generation_failure_discards_every_cached_group_actor(
    tmp_path: Path,
) -> None:
    group_id = "31001"
    session_id = "qq-group-journal-regression"
    workspace = Workspace(
        root=tmp_path / f"group_{group_id}" / "shared",
        chat_kind="group",
        chat_id=group_id,
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
    ).ensure()
    conversation = ConversationIdentity(
        platform="qq",
        chat_kind="group",
        chat_id=group_id,
    )
    journal = GroupConversationJournal(workspace, conversation)

    states: list[SessionState] = []
    identities: list[TurnIdentity] = []
    backends: list[_DiscardableBackend] = []
    for index, actor_id in enumerate(("21001", "21002"), start=1):
        identity = TurnIdentity(
            conversation=conversation,
            sender_user_id=actor_id,
            message_id=f"message-{index}",
        )
        backend = _DiscardableBackend()
        state = SessionState(
            session_id=session_id,
            execution_session_id=f"{session_id}.actor.{actor_id}",
            workspace=Workspace(
                root=workspace.root,
                chat_kind="group",
                chat_id=group_id,
                user_id=actor_id,
                scope=WORKSPACE_SCOPE_GROUP_SHARED,
            ),
            role=Role.USER,
            assistant_mode=AssistantMode.PERFORMANCE,
            runtime=SimpleNamespace(platform_type="qq"),
            session=backend,  # type: ignore[arg-type]
        )
        state.bind_group_turn(identity=identity, journal=journal, turn_context="")
        states.append(state)
        identities.append(identity)
        backends.append(backend)

    journal.append(
        identity=identities[0],
        user_text="durable history",
        assistant_text="reply",
    )
    # Leaving metadata behind makes a missing journal an explicit generation
    # failure instead of a new empty conversation starting at sequence 1.
    journal.path.unlink()

    agent = AcpChatAgent.__new__(AcpChatAgent)
    agent._runtime = SimpleNamespace(platform_type="qq")
    agent._sessions = {session_id: states[0]}
    agent._group_actor_sessions = OrderedDict(
        ((session_id, identity.sender_user_id), state)
        for identity, state in zip(identities, states, strict=True)
    )
    agent._attachment_ack_tasks = {}
    agent._attachment_ack_resource_names = {}

    with pytest.raises(SenderEnvelopeError) as caught:
        agent._activate_turn_identity(
            session=states[0],
            session_id=session_id,
            identity=identities[0],
        )

    assert caught.value.code == "qq_group_journal_unavailable"
    assert agent._group_actor_sessions == {}
    assert session_id not in agent._sessions
    assert [backend.discard_count for backend in backends] == [1, 1]
