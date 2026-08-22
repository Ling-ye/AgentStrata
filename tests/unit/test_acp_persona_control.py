from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from chatcopilot.contracts.identity import AssistantMode, Role
from chatcopilot.contracts.persona_control import (
    PersonaControlSpec,
    PersonaMutationReceipt,
)
from chatcopilot.contracts.persistent_state import has_meaningful_persona
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.core.log_context import pop_log_context
from chatcopilot.middleware.acp.persona_control import handle_persona_control
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder
from chatcopilot.core.workspace_runtime import MiddlewareWorkspaceService, Workspace


class _Conn:
    def __init__(self):
        self.updates = []

    async def session_update(self, **kwargs):
        self.updates.append(kwargs)


class _Host:
    def __init__(self, runtime=None):
        self._conn = _Conn()
        self.finishes = []
        self.runtime_calls = 0
        self.runtime = runtime

    def _finish_turn_task(self, _task, **kwargs):
        self.finishes.append(kwargs)
        if _task is not None:
            _task.finish(**kwargs)

    def _get_or_build_agent_runtime(self):
        self.runtime_calls += 1
        if self.runtime is None:
            raise RuntimeError("chat runtime unavailable")
        return self.runtime


class _Llm:
    model = "chat-persona-test"

    def __init__(self, *, fail=False, residual_text="", decision="set"):
        self.fail = fail
        self.residual_text = residual_text
        self.decision = decision
        self.calls = []

    def chat(self, *, messages, **_kwargs):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("provider unavailable")
        if "route persistent assistant-persona requests" in messages[0]["content"]:
            current = json.loads(messages[-1]["content"])["CURRENT_MESSAGE"]
            if self.decision == "none":
                return ChatResult(
                    content='{"operation":"none","confidence":"low","scope":"default",'
                    '"persona_text":"","residual_text":"","enrich":false,"reason":"normal"}'
                )
            persona_text = current.replace("，解释量子纠缠", "")
            return ChatResult(
                content=json.dumps(
                    {
                        "operation": "set",
                        "confidence": "high",
                        "scope": "default",
                        "persona_text": persona_text,
                        "residual_text": self.residual_text,
                        "enrich": True,
                        "reason": "explicit persistent persona",
                    },
                    ensure_ascii=False,
                ),
                usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            )
        if "PersonaDraftAgent" in messages[0]["content"]:
            request = json.loads(messages[1]["content"])
            tool_messages = [item for item in messages if item.get("role") == "tool"]
            if request["research_required"] and not tool_messages:
                return ChatResult(
                    tool_calls=[
                        {
                            "id": "search-1",
                            "type": "function",
                            "function": {
                                "name": "search_information",
                                "arguments": json.dumps(
                                    {
                                        "query": request["owner_requirement"],
                                        "objective": "核实身份和表达风格",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 8, "total_tokens": 8},
                )
            source_urls = []
            if tool_messages:
                sources = json.loads(tool_messages[-1]["content"])["sources"]
                source_urls = [item["url"] for item in sources[:2]]
            return ChatResult(
                content=json.dumps(
                    {
                        "markdown": (
                            "# 完整人格\n\n"
                            + (
                                "## 现有人格\n"
                                + request["current_persona"]
                                + "\n\n"
                                if request["current_persona"]
                                else ""
                            )
                            + (
                            "## Owner 要求\n"
                            + request["owner_requirement"]
                            + "\n\n## 表达方式\n使用自然中文交流。"
                            )
                        ),
                        "source_urls": source_urls,
                    },
                    ensure_ascii=False,
                ),
                usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            )
        return ChatResult(
            content='{"markdown":"# 人格","source_urls":[]}'
        )


class _Coordinator:
    def __init__(self, *, fail=False):
        self.fail = fail

    def run(self, _request):
        if self.fail:
            raise RuntimeError("search failed")
        return {
            "results": [
                {
                    "items": [
                        {
                            "url": "https://official.example/profile",
                            "title": "官方简介",
                            "content": "公开身份与作品简介。",
                        },
                        {
                            "url": "https://wiki.example/profile",
                            "title": "公开资料",
                            "snippet": "表达风格和人物背景摘要。",
                        },
                    ]
                }
            ]
        }


class _Runtime:
    def __init__(self, *, llm=None, search_fail=False):
        self.llm = llm or _Llm()
        self.research_llm = self.llm
        self.coordinator = _Coordinator(fail=search_fail)

    def build_unified_search_coordinator(self, **_kwargs):
        return self.coordinator


def _runtime():
    return SimpleNamespace(
        platform_type="qq",
        subagents=SimpleNamespace(persona_control=PersonaControlSpec(enabled=True)),
        prompt_profile=BotPromptProfile(
            identity="Test assistant",
            response_style="Return concise test responses.",
        ),
        capability_policies=(),
        skills=(),
    )


def _session(tmp_path, *, role=Role.OWNER, actor="owner"):
    workspace = Workspace(
        root=tmp_path / "group_group-1" / "shared",
        chat_kind="group",
        chat_id="group-1",
        user_id=actor,
        scope=WORKSPACE_SCOPE_GROUP_SHARED,
    ).ensure()
    return SessionState(
        session_id="session-1",
        execution_session_id=f"actor-{actor}",
        workspace=workspace,
        role=role,
        assistant_mode=AssistantMode.GENERAL,
        runtime=_runtime(),
    )


def _turn(session, text, *, turn_task=None):
    return SimpleNamespace(
        session=session,
        session_id="session-1",
        user_text=text,
        message_id="message-1",
        turn_task=turn_task,
        metadata={},
    )


async def _run(host, turn):
    return await handle_persona_control(
        host=host,
        turn=turn,
        update_text=lambda text: text,
        refresh_prompt_plan=lambda _session: None,
    )


def _state(tmp_path, session):
    return MiddlewareWorkspaceService(
        workspace=session.workspace,
        workspace_root=tmp_path,
        platform_type="qq",
    ).resolve_persistent_state()


def _detach_recorder_log_context(recorder: TurnTaskRecorder) -> None:
    """Keep a recorder usable across asyncio.run without leaking test context."""

    token = recorder._log_context_token
    assert token is not None
    pop_log_context(token)
    recorder._log_context_token = None


def test_exact_set_uses_draft_agent_and_survives_restart(tmp_path) -> None:
    host = _Host(_Runtime())
    session = _session(tmp_path)
    response = asyncio.run(_run(host, _turn(session, "/persona set group 使用中文，语气简洁。")))
    assert response.stop_reason == "end_turn"
    assert host.runtime_calls == 1
    assert "使用中文，语气简洁。" in _state(tmp_path, session).persona_snapshot("group")
    restarted = _state(tmp_path, replace(session, workspace=replace(session.workspace, user_id="other")))
    assert "使用中文，语气简洁。" in restarted.persona_snapshot("group")


def test_append_is_agent_authored_full_replacement_and_one_set_receipt(tmp_path) -> None:
    host = _Host(_Runtime())
    session = _session(tmp_path)
    state = _state(tmp_path, session)
    state.persona_set("group", "# 旧人格\n保持简洁")
    recorder = TurnTaskRecorder(
        workspace=session.workspace,
        session_id=session.session_id,
        message_id="message-append",
        user_text="/persona append group 再活泼一点",
    )
    _detach_recorder_log_context(recorder)

    asyncio.run(
        _run(
            host,
            _turn(
                session,
                "/persona append group 再活泼一点",
                turn_task=recorder,
            ),
        )
    )

    saved = state.persona_snapshot("group")
    assert "# 旧人格" in saved
    assert "再活泼一点" in saved
    events = [
        json.loads(line)
        for line in recorder.path.parent.joinpath("events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    mutations = [event for event in events if event["event"] == "persona_mutation"]
    assert [event["data"]["operation"] for event in mutations] == ["set"]


def test_compact_persona_prefix_fails_closed_before_write_without_runtime(tmp_path) -> None:
    host = _Host()
    session = _session(tmp_path)
    text = "/persona你来模仿异世界情绪，回复始终以一句歌词结尾，使用中文回复"
    response = asyncio.run(_run(host, _turn(session, text)))
    saved = _state(tmp_path, session).persona_snapshot("group")
    assert response is not None
    assert "本轮没有保存" in host._conn.updates[-1]["update"]
    assert saved == ""
    assert host.finishes[-1]["status"] == "failed"


@pytest.mark.parametrize(
    "text",
    [
        "置你的人格为鸣潮的莫宁",
        "设置你的人格为鸣潮的莫宁",
        "以后你就是鸣潮的莫宁",
        "你来模仿异世界情绪，回复始终以一句歌词结尾，使用中文回复",
    ],
)
def test_real_qq_phrases_skip_classifier_but_fail_closed_when_research_fails(tmp_path, text) -> None:
    runtime = _Runtime(llm=_Llm(fail=True), search_fail=True)
    host = _Host(runtime)
    session = _session(tmp_path)
    response = asyncio.run(_run(host, _turn(session, text)))
    assert response is not None
    assert _state(tmp_path, session).persona_snapshot("group") == ""
    assert host.finishes[-1]["status"] == "failed"
    assert "本轮没有保存" in host._conn.updates[-1]["update"]


def test_search_failure_leaves_existing_persona_unchanged(tmp_path) -> None:
    host = _Host(_Runtime(search_fail=True))
    session = _session(tmp_path)
    _state(tmp_path, session).persona_set("group", "existing")
    text = "设置你的人格为鸣潮的莫宁"
    asyncio.run(_run(host, _turn(session, text)))
    assert _state(tmp_path, session).persona_snapshot("group").strip() == "existing"
    assert "本轮没有保存" in host._conn.updates[-1]["update"]
    assert host.finishes[-1]["status"] == "failed"


def test_search_failure_records_failed_without_mutation_receipt(tmp_path) -> None:
    host = _Host(_Runtime(search_fail=True))
    session = _session(tmp_path)
    recorder = TurnTaskRecorder(
        workspace=session.workspace,
        session_id=session.session_id,
        message_id="message-observed",
        user_text="设置你的人格为鸣潮的莫宁",
    )
    _detach_recorder_log_context(recorder)
    asyncio.run(
        _run(
            host,
            _turn(
                session,
                "设置你的人格为鸣潮的莫宁",
                turn_task=recorder,
            ),
        )
    )
    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["persona_outcome"]["outcome"] == "failed"
    assert payload["persona_outcome"]["error_code"] == "persona_search_failed"
    events = recorder.path.parent.joinpath("events.jsonl").read_text(encoding="utf-8")
    assert '"event":"persona_draft"' in events
    assert '"error_code":"persona_search_failed"' in events
    assert payload["usage_totals"]["llm_calls"] == 1
    assert payload["llm_calls"][0]["model"] == "chat-persona-test"


def test_failed_base_write_finishes_failed_and_never_claims_saved(tmp_path, monkeypatch) -> None:
    host = _Host(_Runtime())
    session = _session(tmp_path)
    recorder = TurnTaskRecorder(
        workspace=session.workspace,
        session_id=session.session_id,
        message_id="message-failed",
        user_text="/persona set group 不会写入",
    )
    _detach_recorder_log_context(recorder)
    monkeypatch.setattr(
        "chatcopilot.middleware.acp.persona_control.PersonaControlService.execute",
        lambda _self, request: PersonaMutationReceipt(
            ok=False,
            operation=request.operation,
            scope=request.scope,
            error_code="persona_persistence_failed",
        ),
    )
    asyncio.run(
        _run(
            host,
            _turn(
                session,
                "/persona set group 不会写入",
                turn_task=recorder,
            ),
        )
    )
    payload = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["persona_outcome"] == {
        "outcome": "failed",
        "error_code": "persona_persistence_failed",
    }
    assert "已设置" not in host._conn.updates[-1]["update"]


def test_successful_research_writes_one_complete_evidence_backed_draft(tmp_path) -> None:
    host = _Host(_Runtime())
    session = _session(tmp_path)
    text = "你来模仿异世界情绪，回复始终以一句歌词结尾，使用中文回复"
    response = asyncio.run(_run(host, _turn(session, text)))
    saved = _state(tmp_path, session).persona_snapshot("group")
    assert response is not None
    assert session.is_materialized is False
    assert text in saved
    assert saved.startswith("# 完整人格")
    assert "草案 Agent 使用了 2 个公开来源" in host._conn.updates[-1]["update"]


def test_explicit_request_bypasses_intent_model_and_compiles_directly(tmp_path) -> None:
    host = _Host(_Runtime(llm=_Llm(decision="none")))
    session = _session(tmp_path)
    text = "设置你的人格为鸣潮的莫宁"
    asyncio.run(_run(host, _turn(session, text)))
    assert text in _state(tmp_path, session).persona_snapshot("group")
    assert all(
        "route persistent assistant-persona requests" not in call[0]["content"]
        for call in host.runtime.llm.calls
    )


def test_normal_message_skips_persona_runtime_and_continues_to_main_agent(tmp_path) -> None:
    host = _Host(_Runtime(llm=_Llm(fail=True)))
    session = _session(tmp_path)
    response = asyncio.run(_run(host, _turn(session, "解释量子纠缠")))
    assert response is None
    assert not has_meaningful_persona(_state(tmp_path, session).persona_snapshot("group"))
    assert host.finishes == []
    assert host.runtime_calls == 0


def test_who_are_you_skips_persona_runtime(tmp_path) -> None:
    host = _Host(_Runtime(llm=_Llm(fail=True)))
    session = _session(tmp_path)
    response = asyncio.run(_run(host, _turn(session, "你是谁")))
    assert response is None
    assert host.runtime_calls == 0


def test_explicit_composite_research_failure_does_not_forward_partial_turn(tmp_path) -> None:
    host = _Host(_Runtime(llm=_Llm(fail=True)))
    session = _session(tmp_path)
    original = "设置你的人格为鸣潮的莫宁，解释量子纠缠"
    turn = _turn(session, original)

    response = asyncio.run(_run(host, turn))

    assert response is not None
    assert turn.user_text == original
    assert not has_meaningful_persona(_state(tmp_path, session).persona_snapshot("group"))
    assert host.finishes[-1]["status"] == "failed"


def test_clear_requires_actor_bound_second_command(tmp_path) -> None:
    host = _Host(_Runtime())
    session = _session(tmp_path)
    asyncio.run(_run(host, _turn(session, "/persona set group 保留到明确清空")))
    asyncio.run(_run(host, _turn(session, "/persona clear group")))
    assert has_meaningful_persona(_state(tmp_path, session).persona_snapshot("group"))
    asyncio.run(_run(host, _turn(session, "/persona confirm")))
    assert not has_meaningful_persona(_state(tmp_path, session).persona_snapshot("group"))


def test_member_cannot_write_even_with_explicit_compact_command(tmp_path) -> None:
    host = _Host()
    session = _session(tmp_path, role=Role.USER, actor="member")
    asyncio.run(_run(host, _turn(session, "/persona设置你的人格为莫宁")))
    assert not has_meaningful_persona(_state(tmp_path, session).persona_snapshot("group"))
    assert "仅限 Owner" in host._conn.updates[-1]["update"]


def test_group_show_returns_hash_without_raw_persona(tmp_path) -> None:
    host = _Host(_Runtime())
    session = _session(tmp_path)
    secret_persona = "只在保护状态中保存的完整人格正文"
    asyncio.run(_run(host, _turn(session, f"/persona set group {secret_persona}")))
    asyncio.run(_run(host, _turn(session, "/persona show group")))
    shown = host._conn.updates[-1]["update"]
    assert "version=sha256:" in shown
    assert secret_persona not in shown


def test_composite_turn_persists_then_passes_only_exact_residual(tmp_path) -> None:
    residual = "解释量子纠缠"
    host = _Host(_Runtime())
    session = _session(tmp_path)
    original = "设置你的人格为鸣潮的莫宁，解释量子纠缠"
    turn = _turn(session, original)
    response = asyncio.run(_run(host, turn))
    assert response is None
    assert turn.user_text == residual
    assert turn.metadata["journal_user_text"] == original
    assert "已设置 group 人格" in turn.metadata["persona_final_prefix"]
    assert "设置你的人格为鸣潮的莫宁" in _state(tmp_path, session).persona_snapshot("group")
