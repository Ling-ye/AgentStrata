from __future__ import annotations

import json

from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder
from chatcopilot.middleware.runtime.workspace import Workspace


def test_persona_decision_and_clarification_outcome_are_structured(tmp_path) -> None:
    workspace = Workspace(
        root=tmp_path / "p2p_owner",
        chat_kind="p2p",
        chat_id=None,
        user_id="owner",
    ).ensure()
    recorder = TurnTaskRecorder(
        workspace=workspace,
        session_id="session-persona",
        message_id="message-persona",
        user_text="以后就按她那样跟我说话",
    )
    recorder.persona_decision(
        operation="append",
        confidence="medium",
        scope="user",
        reason="depends on previous context",
        source="llm",
        model="chat-test",
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    recorder.set_persona_outcome(outcome="clarification_required")
    recorder.finish(
        status="succeeded",
        progress="人格要求存在歧义。",
        final_text="尚未保存，请使用 /persona 明确重发。",
        stop_reason="end_turn",
    )

    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert payload["persona_outcome"] == {
        "outcome": "clarification_required",
        "error_code": "",
    }
    assert any(step["type"] == "persona_control" for step in payload["steps"])
    assert payload["usage_totals"]["llm_calls"] == 1
