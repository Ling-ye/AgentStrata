from __future__ import annotations

import json

from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.middleware.runtime.tasks import EVENTS_FILENAME, TurnTaskRecorder


def test_persona_manage_uses_generic_structured_tool_observability(tmp_path) -> None:
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
        user_text="以后说话更简洁",
    )
    recorder.tool_started(
        "persona_manage",
        {
            "operation": "set",
            "scope": "default",
            "requirement": "以后说话更简洁",
        },
        span_id="persona-tool",
    )
    recorder.tool_finished(
        "persona_manage",
        True,
        "已设置 user 人格。",
        span_id="persona-tool",
        data={
            "ok": True,
            "data": {
                "outcome": "saved",
                "operation": "set",
                "scope": "user",
                "committed": True,
            },
        },
    )
    recorder.finish(
        status="succeeded",
        progress="人格工具调用完成。",
        final_text="已设置 user 人格。",
        stop_reason="end_turn",
    )

    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (recorder.path.parent / EVENTS_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    finished = next(event for event in events if event["event"] == "tool_finished")
    assert payload["status"] == "succeeded"
    assert payload["tools"][0]["name"] == "persona_manage"
    assert finished["data"]["result"]["data"]["committed"] is True
    assert any(
        step["type"] == "tool" and step["title"] == "persona_manage"
        for step in payload["steps"]
    )
