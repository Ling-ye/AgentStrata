from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from chatcopilot.agent.backends.codex_app_server import AppServerProjector
from chatcopilot.contracts.agent import AgentContentDelta, AgentMessageObserved, LlmCallFinished, SpanFinished, SpanUpdated
from chatcopilot.contracts.cancellation import CancellationRequested
from chatcopilot.core.agent_process import AgentProcessAdapter
from chatcopilot.external_tools.codex_cli.app_server import run_app_server


def projector(events, *, initial_usage=None):
    value = AppServerProjector(model="fixture", iteration=0, trace_id="run-fixture", llm_span_id="backend",
        parent_span_id="actor", context_snapshot_id="context", on_event=events.append,
        on_thread_started=lambda _: None, initial_usage=initial_usage)
    value.bind_thread("thread-fixture")
    value.consume_notification("turn/started", {"threadId": "thread-fixture", "turn": {"id": "turn-fixture"}})
    return value


def emit(value, method, **params):
    value.consume_notification(method, {"threadId": "thread-fixture", "turnId": "turn-fixture", **params})


def test_public_summary_sections_messages_tools_and_final_are_separate():
    events = []
    value = projector(events, initial_usage={})
    emit(value, "item/started", item={"type": "reasoning", "id": "reason"})
    emit(value, "item/reasoning/summaryTextDelta", itemId="reason", summaryIndex=0, delta="Checking")
    emit(value, "item/reasoning/summaryTextDelta", itemId="reason", summaryIndex=1, delta="Resolved")
    value.flush(force=True)
    emit(value, "item/reasoning/textDelta", itemId="reason", delta="hidden-content")
    emit(value, "item/completed", item={"type": "reasoning", "id": "reason",
        "summary": ["Checking", "Resolved"], "content": ["hidden-content"]})
    emit(value, "item/started", item={"type": "agentMessage", "id": "progress", "phase": "commentary", "text": ""})
    emit(value, "item/agentMessage/delta", itemId="progress", delta="Working")
    emit(value, "item/completed", item={"type": "agentMessage", "id": "progress", "phase": "commentary", "text": "Working"})
    emit(value, "item/started", item={"type": "commandExecution", "id": "cmd", "command": "pwd", "cwd": "/workspace"})
    emit(value, "item/commandExecution/outputDelta", itemId="cmd", delta="/workspace")
    emit(value, "item/completed", item={"type": "commandExecution", "id": "cmd", "command": "pwd",
        "aggregatedOutput": "/workspace", "exitCode": 0, "status": "completed"})
    emit(value, "item/completed", item={"type": "mcpToolCall", "id": "mcp", "server": "fixture", "tool": "lookup",
        "arguments": {"query": "topic"}, "result": {"content": ["result"]}, "status": "completed"})
    emit(value, "item/completed", item={"type": "fileChange", "id": "file", "changes": [{"path": "demo", "diff": "+one"}]})
    final = {"type": "agentMessage", "id": "final", "phase": "final_answer", "text": "Done"}
    emit(value, "item/completed", item=final)
    emit(value, "item/completed", item=final)
    emit(value, "item/agentMessage/delta", itemId="final", delta="late")
    emit(value, "thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": 10, "outputTokens": 4,
        "cachedInputTokens": 5, "reasoningOutputTokens": 2, "totalTokens": 14}})
    emit(value, "turn/completed", turn={"id": "turn-fixture", "status": "completed"})
    value.finish(returncode=0)
    assert value.final_text == "Done"
    assert len([e for e in events if isinstance(e, AgentMessageObserved) and e.message_kind == "final"]) == 1
    deltas = [e for e in events if isinstance(e, AgentContentDelta)]
    assert {e.content_kind for e in deltas} == {"message", "public_summary", "command_output"}
    assert [e.section for e in deltas if e.content_kind == "public_summary"] == [0, 1]
    saved = [AgentProcessAdapter().project(e) for e in events]
    assert "hidden-content" not in repr(saved) and "late" not in repr(saved)
    assert "aggregated_output" in repr(saved) and "topic" in repr(saved) and "+one" in repr(saved)
    finished = [e for e in events if isinstance(e, LlmCallFinished)]
    assert len(finished) == 1 and finished[0].usage["total_tokens"] == 14


def test_thread_metadata_is_not_persisted_as_resume_before_turn_acceptance():
    persisted = []
    value = AppServerProjector(model="fixture", iteration=0, trace_id="run", llm_span_id="backend",
        parent_span_id="actor", context_snapshot_id="context", on_event=lambda _: None,
        on_thread_started=persisted.append)
    value.bind_thread("thread-fixture")
    assert persisted == []
    value.consume_notification("turn/started", {"threadId": "thread-fixture", "turn": {"id": "turn-fixture"}})
    value.consume_notification("turn/started", {"threadId": "thread-fixture", "turn": {"id": "turn-fixture"}})
    assert persisted == ["thread-fixture"]


def test_buffered_message_keeps_start_order_and_initial_public_text():
    events = []
    value = projector(events)
    emit(value, "item/started", item={"type": "agentMessage", "id": "message", "phase": "commentary", "text": "first "})
    emit(value, "item/started", item={"type": "reasoning", "id": "reasoning"})
    emit(value, "item/agentMessage/delta", itemId="message", delta="second")
    value.flush(force=True)
    assert isinstance(events[0], AgentMessageObserved) and events[0].phase == "start"
    assert events[1].kind == "reasoning"
    assert next(e.text for e in events if isinstance(e, AgentContentDelta)) == "first second"


def test_resumed_usage_uses_baseline_and_never_claims_unknown_thread_total():
    for baseline, expected in [(None, None), ({"inputTokens": 100, "outputTokens": 30, "totalTokens": 130}, 15)]:
        events = []
        value = projector(events, initial_usage=baseline)
        emit(value, "thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": 110,
            "outputTokens": 35, "totalTokens": 145, "cachedInputTokens": 0, "reasoningOutputTokens": 0}})
        emit(value, "turn/completed", turn={"id": "turn-fixture", "status": "completed"})
        value.finish(returncode=0)
        usage = next(e.usage for e in events if isinstance(e, LlmCallFinished))
        assert (usage["total_tokens"] if usage else None) == expected


def test_plan_revisions_remain_visible_and_terminal_error_has_provider_detail():
    events = []
    value = projector(events)
    for status in ("pending", "inProgress", "completed"):
        emit(value, "turn/plan/updated", plan=[{"step": "check", "status": status}])
    emit(value, "turn/completed", turn={"id": "turn-fixture", "status": "failed", "error": {"message": "fixture failure"}})
    value.finish(returncode=0)
    plans = [e for e in events if isinstance(e, SpanUpdated) and e.kind == "plan"]
    assert [e.revision for e in plans] == [1, 2, 3]
    finished = next(e for e in events if isinstance(e, SpanFinished) and e.kind == "plan")
    assert not finished.ok and finished.data["output"]["plan"][0]["status"] == "completed"
    assert value.failure_detail == "fixture failure"


def test_cancel_flushes_bounded_observed_text_and_ignores_other_turns():
    events = []
    value = projector(events)
    emit(value, "item/started", item={"type": "commandExecution", "id": "cmd", "command": "test"})
    emit(value, "item/commandExecution/outputDelta", itemId="cmd", delta="x" * 100000)
    emit(value, "item/commandExecution/outputDelta", itemId="cmd", delta="wrong-turn", turnId="other")
    value.fail(reason="cancelled")
    assert "wrong-turn" not in repr(events)
    delta = next(e for e in events if isinstance(e, AgentContentDelta))
    final = next(e for e in events if isinstance(e, SpanFinished) and e.kind == "command")
    assert len(delta.text.encode()) == 48 * 1024 and delta.capture_state == "truncated"
    assert final.data["status"] == "cancelled"
    assert final.data["output"]["aggregated_output"] == delta.text


_SERVER = '''
import json,sys,time
mode=sys.argv[1]
def send(value): print(json.dumps(value), flush=True)
for raw in sys.stdin:
 r=json.loads(raw);method=r.get('method');params=r.get('params',{})
 if method=='initialized': continue
 if method=='initialize':
  assert params['capabilities']['experimentalApi'] is True
  send({'id':r['id'],'result':{}})
 elif method in ('thread/start','thread/resume'):
  if method=='thread/resume': assert params['excludeTurns'] is True
  send({'id':r['id'],'result':{'thread':{'id':params.get('threadId','thread-fixture')},'instructionSources':[]}})
 elif method=='turn/start':
  if mode=='disconnect': sys.exit(0)
  send({'id':r['id'],'result':{'turn':{'id':'turn-fixture'}}})
  send({'method':'turn/started','params':{'threadId':'thread-fixture','turn':{'id':'turn-fixture'}}})
  if mode=='interaction':
   send({'id':91,'method':'item/permissions/requestApproval','params':{}})
  elif mode=='complete':
   send({'method':'item/completed','params':{'threadId':'thread-fixture','turnId':'turn-fixture',
     'item':{'id':'answer','type':'agentMessage','phase':'final_answer','text':'done'}}})
   send({'method':'turn/completed','params':{'threadId':'thread-fixture','turn':{'id':'turn-fixture','status':'completed'}}})
 elif method=='turn/interrupt':
  send({'id':r['id'],'result':{}})
  send({'method':'turn/completed','params':{'threadId':'thread-fixture','turn':{'id':'turn-fixture','status':'interrupted'}}})
'''


def run_fixture(tmp_path, mode, *, on_poll=lambda: None, thread_id=""):
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    events, threads = [], []
    result = run_app_server([sys.executable, str(script), mode], cwd=tmp_path, env=dict(os.environ),
        prompt="fixture", model="model", effort="medium", thread_id=thread_id, image_paths=(), timeout_seconds=2,
        on_notification=lambda method, params: events.append((method, params)), on_thread=threads.append, on_poll=on_poll)
    return result, threads, events


@pytest.mark.parametrize("thread_id", ["", "thread-fixture"])
def test_stdio_handshake_new_and_resume(tmp_path, thread_id):
    result, threads, events = run_fixture(tmp_path, "complete", thread_id=thread_id)
    assert result.returncode == 0 and threads == ["thread-fixture"]
    assert any(method == "item/completed" for method, _ in events)


@pytest.mark.parametrize("mode,match", [("disconnect", "disconnected"), ("interaction", "interaction rejected")])
def test_stdio_disconnect_and_permission_requests_fail_without_replay(tmp_path, mode, match):
    with pytest.raises(RuntimeError, match=match):
        run_fixture(tmp_path, mode)


def test_stdio_cancellation_and_timeout_are_bounded(tmp_path):
    started = time.monotonic()
    def cancel():
        if time.monotonic() - started > .25:
            raise CancellationRequested()
    with pytest.raises(CancellationRequested):
        run_fixture(tmp_path, "wait", on_poll=cancel)
    assert time.monotonic() - started < 4
    with pytest.raises(subprocess.TimeoutExpired):
        run_fixture(tmp_path, "wait")
