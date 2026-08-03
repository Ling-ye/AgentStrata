from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from chatcopilot.contracts import AssistantMode, Role
from chatcopilot.contracts.model_selection import (
    CodeModelSelection,
    MODEL_SELECTION_SCOPE_ONCE,
    MODEL_SELECTION_SOURCE_PROFILE,
)
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.runtime.workspace import Workspace


class _FakeAgentSession:
    def __init__(self) -> None:
        self._messages: list[dict[str, str]] = []
        self.system_baseline = ""

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        self._messages.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )

    def snapshot_messages(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._messages]

    def set_system_baseline(self, value: str) -> None:
        self.system_baseline = value


def _state(tmp_path: Path) -> SessionState:
    workspace = Workspace(
        root=tmp_path / "workspace",
        chat_kind="p2p",
        chat_id="chat-1",
        user_id="owner-1",
    ).ensure()
    return SessionState(
        session_id="sid-lazy",
        workspace=workspace,
        role=Role.OWNER,
        assistant_mode=AssistantMode.GENERAL,
        runtime=SimpleNamespace(),
        session=None,
    )


def test_control_plane_session_buffers_and_replays_exchanges(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.record_exchange("status?", "delegated")

    assert not state.is_materialized
    assert state.message_count() == 2
    assert state.transcript_path is not None
    transcript = state.transcript_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(transcript[1])["content"] == "status?"

    agent_session = _FakeAgentSession()
    state.attach_session(agent_session)

    assert state.is_materialized
    assert state.require_session() is agent_session
    assert agent_session.message_count == 2
    assert agent_session._messages[1]["content"] == "delegated"


def test_control_plane_mode_change_applies_when_session_materializes(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.set_assistant_mode(AssistantMode.PERFORMANCE, "control baseline")
    assert state.assistant_mode == AssistantMode.PERFORMANCE

    agent_session = _FakeAgentSession()
    state.attach_session(agent_session)
    state.set_assistant_mode(
        AssistantMode.PERFORMANCE,
        "materialized baseline",
        session_dynamic_tail="persona",
    )

    assert agent_session.system_baseline == "materialized baseline\n\npersona"


def test_one_shot_code_model_is_consumed_only_by_matching_selection(tmp_path: Path) -> None:
    state = _state(tmp_path)
    selection = CodeModelSelection(
        provider="codex_cli",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        scope=MODEL_SELECTION_SCOPE_ONCE,
        source=MODEL_SELECTION_SOURCE_PROFILE,
        profile="sol-high",
    )
    state.set_code_model_selection(selection)

    state.consume_code_model_once(
        CodeModelSelection(
            provider="codex_cli",
            model="gpt-5.5",
            reasoning_effort="medium",
        )
    )
    assert state.code_model_once == selection

    state.consume_code_model_once(selection)
    assert state.code_model_once is None


def test_workspace_refresh_can_copy_code_model_overrides(tmp_path: Path) -> None:
    source = _state(tmp_path / "source")
    target = _state(tmp_path / "target")
    selection = CodeModelSelection(
        provider="codex_cli",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        source=MODEL_SELECTION_SOURCE_PROFILE,
        profile="sol-high",
    )
    source.set_code_model_selection(selection)

    target.copy_code_model_state_from(source)

    assert target.code_model_selection == selection
