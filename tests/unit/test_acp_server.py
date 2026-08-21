from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chatcopilot.contracts.agent import AgentResult, FinalText, TurnError
from chatcopilot.contracts.model_selection import CodeModelSelection
from chatcopilot.core.config import ChatConfig
from chatcopilot.core.model_selection import CODE_MODEL_SELECTION_METADATA_KEY
from chatcopilot.middleware.acp.server import AcpChatAgent
from chatcopilot.middleware.runtime.workspace import Workspace


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        bot_id="lazy-bot",
        instance_id="lazy-bot",
        skills=(),
        tool_packs=(),
        exclude_tools=(),
        rag_sources=(),
        mcp_servers=(object(),),
        subagents=SimpleNamespace(),
        agent_backend="native",
        spec=SimpleNamespace(llm=SimpleNamespace(env_prefix="CHATCOPILOT_LAZYTEST")),
    )


def test_acp_agent_construction_does_not_build_agent_runtime() -> None:
    with (
        mock.patch(
            "chatcopilot.middleware.acp.server.load_config",
            return_value=ChatConfig(),
        ),
        mock.patch("chatcopilot.middleware.acp.server.build_agent_runtime") as build,
    ):
        agent = AcpChatAgent(runtime=_runtime())

    build.assert_not_called()
    assert agent._agent_runtime is None


def test_first_chat_runtime_materialization_is_singleton() -> None:
    built = SimpleNamespace(tools=[object()], close=lambda: None)
    with (
        mock.patch(
            "chatcopilot.middleware.acp.server.load_config",
            return_value=ChatConfig(),
        ),
        mock.patch(
            "chatcopilot.middleware.acp.server.build_agent_runtime",
            return_value=built,
        ) as build,
    ):
        agent = AcpChatAgent(runtime=_runtime())
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda _: agent._get_or_build_agent_runtime(), range(8)))

    assert all(value is built for value in values)
    build.assert_called_once()


def test_session_creation_is_control_plane_only(tmp_path: Path) -> None:
    workspace = Workspace(
        root=tmp_path / "workspace",
        chat_kind="p2p",
        chat_id="chat-1",
        user_id="owner-1",
    ).ensure()
    with (
        mock.patch(
            "chatcopilot.middleware.acp.server.load_config",
            return_value=ChatConfig(),
        ),
        mock.patch("chatcopilot.middleware.acp.server.build_agent_runtime") as build,
    ):
        agent = AcpChatAgent(runtime=_runtime())
        session = agent._build_session(session_id="sid-control", ws=workspace)

    assert not session.is_materialized
    assert session.routing_config is agent._chat_config.routing
    build.assert_not_called()


def test_llm_error_keeps_raw_diagnostic_private_and_delivers_safe_text(
    tmp_path: Path,
) -> None:
    raw_error = (
        "Codex CLI failed: 401 Unauthorized; stderr=refresh token already used "
        "secret-token-value"
    )
    safe_text = "Codex 登录已失效，请让管理员重新完成机器人独立登录。"

    class _Conn:
        def __init__(self) -> None:
            self.updates = []

        async def session_update(self, *, session_id, update) -> None:
            self.updates.append((session_id, update))

    class _Recorder:
        task_id = "task_test"

        def __init__(self) -> None:
            self.events = []
            self.progress = []
            self.finished = None

        def record_event(self, event_type, payload) -> None:
            self.events.append((event_type, payload))

        def write(self, *, progress) -> None:
            self.progress.append(progress)

        def finish(self, **payload) -> None:
            self.finished = payload

    class _Session:
        def run_task(self, task, *, on_event):
            on_event(TurnError(code="codex_cli_failed", message=raw_error))
            on_event(FinalText(safe_text))
            return AgentResult(final_text=safe_text, stop_reason="llm_error")

    workspace = Workspace(
        root=tmp_path / "workspace",
        chat_kind="p2p",
        chat_id="chat-1",
        user_id="owner-1",
    ).ensure()
    backend_session = _Session()
    session = SimpleNamespace(
        debug_mode=False,
        workspace=workspace,
        require_session=lambda: backend_session,
        persist_transcript=lambda: None,
    )
    recorder = _Recorder()
    agent = AcpChatAgent.__new__(AcpChatAgent)
    agent._conn = _Conn()

    asyncio.run(
        agent._run_agent_turn(
            session,
            "sid-safe-error",
            "hello",
            "message-1",
            turn_task=recorder,
        )
    )

    outbound = [update.content.text for _, update in agent._conn.updates]
    assert outbound == [safe_text]
    assert raw_error not in "\n".join(outbound)
    assert recorder.progress == ["执行失败（错误代码：codex_cli_failed）。"]
    assert recorder.events[0] == (
        "turn_error",
        {"code": "codex_cli_failed", "message": raw_error},
    )
    assert recorder.events[1] == (
        "flow_transition",
        {
            "kind": "delivery.session_update",
            "source_layer": "delivery",
            "target_layer": "transport",
            "status": "succeeded",
            "evidence_level": "observed",
            "title": "ACP 已发出最终 session_update",
            "summary": "该边界不证明 QQ 客户端已显示或用户已读取。",
            "decision": {
                "code": "session_update_emitted",
                "authoritative": False,
            },
            "payload": {"text_length": len(safe_text)},
        },
    )
    assert recorder.finished is not None
    assert recorder.finished["final_text"] == safe_text
    assert recorder.finished["error"] == raw_error


def test_once_model_selection_is_consumed_only_after_run_task_returns(
    tmp_path: Path,
) -> None:
    class _Conn:
        def __init__(self) -> None:
            self.updates = []

        async def session_update(self, *, session_id, update) -> None:
            self.updates.append((session_id, update))

    class _BackendSession:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail
            self.tasks = []

        def run_task(self, task, *, on_event):
            self.tasks.append(task)
            if self.fail:
                raise RuntimeError("test failure")
            on_event(FinalText("done"))
            return AgentResult(final_text="done", stop_reason="end_turn")

    class _SessionState:
        debug_mode = False

        def __init__(self, backend, workspace, selection) -> None:
            self._backend = backend
            self.workspace = workspace
            self.selection = selection
            self.consumed = []

        def require_session(self):
            return self._backend

        def persist_transcript(self) -> None:
            return None

        def effective_code_model_selection(self, _default):
            return self.selection

        def consume_code_model_once(self, selection) -> None:
            self.consumed.append(selection)

    workspace = Workspace(
        root=tmp_path / "workspace",
        chat_kind="p2p",
        chat_id="chat-model-selection",
        user_id="owner-model-selection",
    ).ensure()
    selection = CodeModelSelection(
        provider="codex_cli",
        model="gpt-5.6-sol",
        reasoning_effort="max",
        scope="once",
        source="profile",
        profile="sol-max",
    )
    config = ChatConfig()
    config.routing.code_model = "gpt-5.6-terra"
    config.routing.code_reasoning_effort = "medium"
    agent = AcpChatAgent.__new__(AcpChatAgent)
    agent._conn = _Conn()
    agent._runtime = SimpleNamespace(agent_backend="codex")
    agent._chat_config = config

    successful_backend = _BackendSession()
    successful_session = _SessionState(
        successful_backend,
        workspace,
        selection,
    )
    asyncio.run(
        agent._run_agent_turn(
            successful_session,
            "sid-success",
            "hello",
            "message-success",
        )
    )

    assert successful_backend.tasks[0].metadata[
        CODE_MODEL_SELECTION_METADATA_KEY
    ] == selection.to_payload()
    assert successful_session.consumed == [selection]
    assert config.routing.code_model == "gpt-5.6-terra"

    failed_backend = _BackendSession(fail=True)
    failed_session = _SessionState(failed_backend, workspace, selection)
    asyncio.run(
        agent._run_agent_turn(
            failed_session,
            "sid-failure",
            "hello",
            "message-failure",
        )
    )

    assert failed_session.consumed == []


def test_rejected_explicit_memory_never_retries_or_false_reports(
    tmp_path: Path,
) -> None:
    class _Conn:
        def __init__(self) -> None:
            self.updates = []

        async def session_update(self, *, session_id, update) -> None:
            self.updates.append(update.content.text)

    class _Backend:
        def __init__(self) -> None:
            self.tasks = []
            self.corrections = []

        def run_task(self, task, *, on_event):
            self.tasks.append(task)
            on_event(FinalText("好的，已经记住。"))
            return AgentResult(final_text="好的，已经记住。", stop_reason="end_turn")

        def record_exchange(self, user_text, assistant_text) -> None:
            self.corrections.append((user_text, assistant_text))

    class _State:
        debug_mode = False
        role = SimpleNamespace(value="user")

        def __init__(self, backend) -> None:
            self.backend = backend
            self.workspace = Workspace(
                root=tmp_path / "p2p_member-1",
                chat_kind="p2p",
                chat_id=None,
                user_id="member-1",
            ).ensure()

        def require_session(self):
            return self.backend

        def persist_transcript(self) -> None:
            return None

    backend = _Backend()
    state = _State(backend)
    agent = AcpChatAgent.__new__(AcpChatAgent)
    agent._conn = _Conn()
    asyncio.run(
        agent._run_agent_turn(
            state,
            "sid-rejected-memory",
            "记住 access_" + "token=example-value",
            "message-rejected-memory",
        )
    )

    assert len(backend.tasks) == 1
    assert backend.tasks[0].metadata.get("persistence_receipt_retry") is None
    assert "未保存这条记忆" in agent._conn.updates[-1]
    assert backend.corrections
    assert "未保存这条记忆" in backend.corrections[-1][1]
