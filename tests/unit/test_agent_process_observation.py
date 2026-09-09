from __future__ import annotations

import json
from dataclasses import replace

import pytest

from chatcopilot.agent.backends.codex_events import CodexJsonlProjector
from chatcopilot.agent.langgraph_session import LangGraphAgentSession
from chatcopilot.agent.process import ProcessMessage
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.contracts.agent import (
    AgentMessageObserved, AgentTask, ContextSnapshotPrepared, FinalText,
    LlmCallFinished, LlmCallStarted, SpanFinished, SpanStarted, ToolFinished, ToolStarted,
)
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.core.agent_process import AgentProcessAdapter
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.gateway.observation_queries import RunFilter, events, history
from chatcopilot.gateway.observations import RunObserver
from tests.prompt_plan_fixture import prompt_plan
from tests.unit.test_observation_workbench import make_run, recorded as _recorded


@pytest.fixture
def recorded(tmp_path):
    return _recorded.__wrapped__(tmp_path)


@pytest.mark.parametrize('session_type', [AgentSession, LangGraphAgentSession])
def test_actual_rounds_and_filtered_tool_messages_reach_observation_index(recorded, session_type):
    state, generation, recorder = recorded
    run_id = make_run(recorded)
    calls = []

    class Model:
        model = 'process-fixture'

        def chat(self, **kwargs):
            calls.append(kwargs['messages'].copy())
            if len(calls) == 1:
                return ChatResult(content='querying', tool_calls=[{'id': 'real-call-a', 'type': 'function',
                    'function': {'name': 'echo', 'arguments': '{"value":"requested"}'}}])
            return ChatResult(content='answer', usage={'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14})

    tool = ToolDef(name='echo', summary='echo', category='dev.files', input_schema=object_schema(additional_properties=True),
        output_schema=object_schema(additional_properties=True), handler=lambda args, ctx: ToolResult(ok=True, summary='raw-output', data=args))
    session = session_type(
        session_id="one",
        llm=Model(),
        executor=ToolExecutor(caller_role_hint="owner", tools=[tool]),
        tools_schema=[],
        prompt_plan=prompt_plan("baseline"),
        tool_payload_filter=lambda data: {**data, "summary": "model-output"},
    )
    observer = RunObserver(state, generation, run_id, agent_stage_span_id='host:actor')
    captured = []

    def emit(event):
        captured.append(event)
        observer(event)

    result = session.run_task(AgentTask(text='request', metadata={'trace_id': run_id, 'parent_span_id': 'host:actor'}), on_event=emit)
    assert result.final_text == 'answer'
    models = [x for x in captured if isinstance(x, LlmCallStarted)]
    started = next(x for x in captured if isinstance(x, ToolStarted))
    finished = next(x for x in captured if isinstance(x, ToolFinished))
    assert started.tool_call_id == finished.tool_call_id == 'real-call-a'
    assert started.model_span_id == finished.model_span_id == models[0].span_id
    assert finished.data.get('summary') == 'raw-output', finished.data
    assert json.loads(finished.model_result['content'])['summary'] == 'model-output'
    assert any(x.get('tool_call_id') == 'real-call-a' and 'model-output' in x['content'] for x in calls[1])
    saved = events(recorder.store, run_id)['observations']
    assert {x['data'].get('process_kind') for x in saved} >= {'model_call', 'tool', 'message', 'context'}
    snapshots = [x for x in captured if isinstance(x, ContextSnapshotPrepared)]
    assert [x.iteration for x in snapshots] == [0, 1]
    tool_body = next(x for x in saved if x['kind'] == 'ToolFinished')
    body = recorder.store.body(run_id, tool_body['body_ref'])['payload']
    assert body['model_result']['tool_call_id'] == 'real-call-a'
    assert all(x['data'].get('stage_span_id') == 'host:actor' for x in saved if x['kind'] != 'run_state')
    assert history(recorder.store, RunFilter())['runs'][0]['total_tokens'] == 14


def test_codex_adapter_records_public_items_without_delivery_events():
    captured = []
    projector = CodexJsonlProjector(model='fixture', iteration=0, trace_id='t', llm_span_id='exec',
        parent_span_id='actor', context_snapshot_id='ctx', on_event=captured.append, on_thread_started=lambda _: None)
    items = [
        {'id': 'cmd', 'type': 'command_execution', 'command': 'python -V', 'aggregated_output': 'Python', 'exit_code': 0},
        {'id': 'files', 'type': 'file_change', 'changes': [{'path': 'sample.py', 'kind': 'update'}]},
        {'id': 'mcp', 'type': 'mcp_tool_call', 'server': 'sample', 'tool': 'echo', 'arguments': {'a': 1}, 'result': 'one'},
        {'id': 'search', 'type': 'web_search', 'query': 'sample query', 'results': [{'title': 'sample'}]},
        {'id': 'plan', 'type': 'todo_list', 'items': [{'text': 'read file', 'completed': True}]},
        {'id': 'thought', 'type': 'reasoning', 'text': 'private reasoning', 'summary': 'public explanation'},
    ]
    for item in items:
        projector.consume_line(json.dumps({'type': 'item.completed', 'item': item}))
    for event_type, text in [('item.started', 'working'), ('item.updated', 'working now'), ('item.completed', 'done')]:
        projector.consume_line(json.dumps({'type': event_type, 'item': {'id': 'msg', 'type': 'agent_message', 'text': text}}))
    projector.consume_line(json.dumps({'type': 'item.completed', 'item': {'id': 'msg', 'type': 'agent_message', 'text': 'done'}}))
    projector.finish(returncode=0)
    assert not any(isinstance(x, (SpanStarted, FinalText)) for x in captured)
    messages = [x for x in captured if isinstance(x, AgentMessageObserved)]
    assert len(messages) >= 2
    assert len({x.span_id for x in messages}) == 1
    assert messages[-1].text == 'done'
    assert projector.final_text == 'done'
    adapter = AgentProcessAdapter()
    records = [adapter.project(x) for x in captured]
    assert {x.event['data']['process_kind'] for x in records} >= {
        'command', 'file_change', 'mcp_tool', 'web_search', 'plan', 'reasoning', 'message', 'backend_execution'}
    assert 'private reasoning' not in repr(records)
    assert 'public explanation' in repr(records)
    assert 'sample.py' in repr(records) and 'sample query' in repr(records)
    assert next(x for x in captured if isinstance(x, LlmCallFinished)).execution_kind == 'backend_execution'


def test_live_message_revisions_paginate_and_duplicate_completion_is_idempotent(recorded):
    state, generation, recorder = recorded
    run_id = make_run(recorded)
    observer = RunObserver(state, generation, run_id, agent_stage_span_id='actor')
    message = AgentMessageObserved(text='first', message_id='msg', trace_id=run_id, span_id='msg',
        parent_span_id='model', phase='update', revision=1, status='running')
    observer(message)
    observer(replace(message, text='second', revision=2))
    end = replace(message, text='complete', phase='finish', revision=3, status='succeeded')
    observer(end)
    observer(end)
    cursor, rows = 0, []
    while True:
        page = events(recorder.store, run_id, after=cursor, limit=2)
        rows.extend(page['observations'])
        cursor = page['next_cursor']
        if not page['has_more']:
            break
    observed = [x for x in rows if x['kind'] == 'AgentMessageObserved']
    assert [x['data']['revision'] for x in observed] == [1, 2, 3]
    assert 'complete' not in repr([x['data'] for x in observed])
    assert recorder.store.body(run_id, observed[-1]['body_ref'])['payload']['text'] == 'complete'
    other = make_run(recorded, 'other')
    assert recorder.store.body(other, observed[-1]['body_ref']) is None


def test_observation_stream_is_bounded_and_does_not_emit_delivery(monkeypatch):
    captured = []
    ticks = iter(range(1000))
    monkeypatch.setattr('chatcopilot.agent.process.time.monotonic', lambda: next(ticks) * 2)
    message = ProcessMessage(captured.append, trace_id='t', parent_span_id='model', backend='native', depth=0)
    for _ in range(100):
        message.append('x' * 1024)
    message.finish()
    assert len(captured) == 33
    assert all(isinstance(x, AgentMessageObserved) for x in captured)
    assert captured[-1].capture_state == 'truncated'
    assert len(captured[-1].text.encode()) <= 48 * 1024
    assert len({x.message_id for x in captured}) == 1


def test_unidentified_codex_calls_do_not_merge_by_tool_name():
    captured = []
    projector = CodexJsonlProjector(model='fixture', iteration=0, trace_id='t', llm_span_id='exec',
        parent_span_id='actor', context_snapshot_id='ctx', on_event=captured.append, on_thread_started=lambda _: None)
    for _ in range(2):
        projector.consume_line(json.dumps({'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'tool': 'same'}}))
    assert len({x.span_id for x in captured if isinstance(x, SpanFinished)}) == 2
    assert not any(isinstance(x, SpanStarted) for x in captured)


def test_adapter_failure_marks_capture_gap_without_changing_run(recorded, monkeypatch):
    state, generation, recorder = recorded
    run_id = make_run(recorded)
    observer = RunObserver(state, generation, run_id, agent_stage_span_id='host:actor')
    def fail(_event):
        raise ValueError('invalid process payload')
    monkeypatch.setattr(observer.process_adapter, 'project', fail)
    observer(LlmCallStarted(model='fixture', iteration=0))
    assert state.get_run(run_id).state == 'running'
    assert history(recorder.store, RunFilter())['runs'][0]['capture_state'] == 'capture_failed'
    gap = events(recorder.store, run_id)['observations'][-1]
    assert gap['kind'] == 'AgentProcessCaptureFailed'
    assert gap['data']['stage_span_id'] == 'host:actor'


def test_codex_telemetry_limit_never_suppresses_completed_reply():
    captured = []
    projector = CodexJsonlProjector(model='fixture', iteration=0, trace_id='t', llm_span_id='exec',
        parent_span_id='actor', context_snapshot_id='ctx', on_event=captured.append, on_thread_started=lambda _: None)
    for n in range(500):
        projector.consume_line(json.dumps({'type': 'item.updated', 'item': {'id': f'command-{n}', 'type': 'command_execution', 'command': 'fixture'}}))
    projector.consume_line(json.dumps({'type': 'item.completed', 'item': {'id': 'reply', 'type': 'agent_message', 'text': 'reply after telemetry limit'}}))
    projector.finish(returncode=0)
    assert projector.final_text == 'reply after telemetry limit'
    finish = next(x for x in captured if isinstance(x, LlmCallFinished))
    assert finish.ok
    assert finish.visible_response['content'] == projector.final_text
    assert any(isinstance(x, SpanFinished) and x.kind == 'provider_omission' for x in captured)
