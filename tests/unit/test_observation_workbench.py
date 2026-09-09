from __future__ import annotations

import hashlib
import json
import logging
import os
import time

import pytest

from chatcopilot.contracts.agent import ContextSnapshotPrepared, LlmCallStarted, LlmCallFinished, ToolStarted, ToolFinished
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.botspec.inspection import configuration_projection
from chatcopilot.gateway.observation_queries import RunFilter, detail, events, history, metrics
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway.observation_store import BODY_LIMIT, ObservationStore, RETENTION_SECONDS
from chatcopilot.gateway.observations import RunObserver
from chatcopilot.gateway.state_store import GatewayStateError, GatewayStateStore


@pytest.fixture
def recorded(tmp_path):
    state = GatewayStateStore(tmp_path / 'gateway')
    generation = state.acquire_writer_generation()
    recorder = ObservationRecorder(state, generation, configuration={'layers': [], 'entities': [], 'backend': 'native'})
    return state, generation, recorder


def make_run(recorded, suffix='one', *, at=None, complete=False):
    state, generation, recorder = recorded
    now = time.time() if at is None else at
    run_id = 'run-' + suffix
    session_id = 'session-' + suffix
    state.create_session(generation=generation, session_id=session_id,
                         account=ChannelAccountRef('fixture', 'account'), conversation=ConversationRef('p2p', suffix))
    state.begin_run(generation=generation, session_id=session_id, run_id=run_id,
                    input_fingerprint=hashlib.sha256(suffix.encode()).hexdigest(), now=now)
    recorder.store.bind_run(run_id, config_id=recorder.config_id, backend='native', model='fixture-model', role='owner')
    state.start_run(generation=generation, session_id=session_id, run_id=run_id, now=now + 1)
    if complete:
        state.finish_run(generation=generation, session_id=session_id, run_id=run_id, outcome='completed',
                         result={'final_text': 'fixture result'}, now=now + 4)
    return run_id


def test_closed_console_records_lifecycle_and_recovers_without_invented_steps(recorded):
    state, generation, recorder = recorded
    run_id = make_run(recorded)
    assert detail(recorder.store, run_id)['run']['state'] == 'running'
    state.acquire_writer_generation()
    fresh = ObservationRecorder(state, generation + 1)
    item = detail(fresh.store, run_id)
    assert item['run']['state'] == 'recovery_required'
    assert [event['data'] for event in item['observations']] == [{'code': None}] * 3
    assert item['run']['config_id'] == recorder.config_id


def test_history_search_paginates_all_records_and_preserves_selected_detail(recorded):
    _, _, recorder = recorded
    for index in range(63):
        make_run(recorded, str(index), complete=True)
    first = history(recorder.store, RunFilter(limit=50))
    second = history(recorder.store, RunFilter(page=2))
    assert len(first['runs']) == 50 and len(second['runs']) == 13 and first['has_more']
    assert first['total'] == 63
    assert not ({row['run_id'] for row in first['runs']} & {row['run_id'] for row in second['runs']})
    assert history(recorder.store, RunFilter(search='run-0'))['runs'][0]['run_id'] == 'run-0'
    assert detail(recorder.store, 'run-0')['run']['state'] == 'completed'


def test_unversioned_records_are_read_without_synthetic_call_identifiers(recorded):
    _, _, recorder = recorded
    run_id = make_run(recorded, complete=True)
    for kind, status in [('response_dispatch', 'running'), ('channel_returned', 'succeeded')]:
        recorder.store.append(run_id, {'kind': kind, 'status': status, 'data': {}})
    before = recorder.store.database.read_bytes()
    page = events(recorder.store, run_id, limit=3)
    more = events(recorder.store, run_id, after=page['next_cursor'])
    calls = [event for event in [*page['observations'], *more['observations']]
             if event['kind'] in {'response_dispatch', 'channel_returned'}]
    assert len(calls) == 2
    assert all(event['span_id'] is None and event['trace_id'] is None for event in calls)
    assert all(event['phase'] == '' and 'outbound_id' not in event['data'] for event in calls)
    assert recorder.store.database.read_bytes() == before


def test_duplicate_events_nested_token_usage_and_parallel_durations(recorded):
    state, generation, recorder = recorded
    run_id = make_run(recorded, complete=True)
    observer = RunObserver(state, generation, run_id)
    for number in range(2):
        observer(LlmCallStarted('fixture-model', number, trace_id='trace', span_id=f'llm-{number}'))
        finish = LlmCallFinished('fixture-model', number, trace_id='trace', span_id=f'llm-{number}',
                                parent_span_id='parent', usage={'input_tokens': 100, 'output_tokens': 10,
                                                              'input_tokens_details': {'cached_tokens': 25}})
        observer(finish)
        observer(finish)
    report = metrics(recorder.store, RunFilter())
    assert report['totals']['model_calls'] == 2
    assert report['totals']['total_tokens'] == 220
    assert report['totals']['cached_tokens'] == 50
    assert report['totals']['mean_ms'] == 3000
    assert report['totals']['p95_ms'] is None


def test_metric_threshold_and_filtered_components(recorded):
    state, generation, recorder = recorded
    for index in range(21):
        run_id = make_run(recorded, str(index), complete=True)
        observer = RunObserver(state, generation, run_id)
        observer(ToolStarted('lookup', {'query': 'example'}, trace_id=run_id, span_id='tool'))
        observer(ToolFinished('lookup', False, '', error='timeout', trace_id=run_id, span_id='tool'))
    report = metrics(recorder.store, RunFilter(component='tool:lookup'))
    assert report['totals']['sample_count'] == 21
    assert report['totals']['p95_ms'] == 3000
    assert report['trends'][0]['sample_count'] == 21
    assert report['trends'][0]['p95_ms'] == 3000
    assert report['components'][0]['failures'] == 21
    assert history(recorder.store, RunFilter(component='tool:absent'))['total'] == 0


def test_private_body_values_scope_and_reasoning_omission(recorded):
    state, generation, recorder = recorded
    run_id = make_run(recorded)
    observer = RunObserver(state, generation, run_id)
    observer(ContextSnapshotPrepared('context', 'native', 'fixture', 1,
        ({'role': 'assistant', 'content': 'visible', 'reasoning_content': 'private-thought'},),
        ({'role': 'user', 'content': 'api_key=fixture-credential'},)))
    event = detail(recorder.store, run_id)['observations'][-1]
    assert event['body_ref'] and 'visible' not in json.dumps(event)
    body = recorder.store.body(run_id, event['body_ref'])
    assert 'private-thought' not in json.dumps(body)
    assert body['payload']['effective_messages'][0]['content'] == 'api_key=fixture-credential'
    assert 'visible' in json.dumps(body)
    assert recorder.store.body('other-run', event['body_ref']) is None
    with pytest.raises(ValueError):
        recorder.store.body(run_id, '../outside')


def test_retention_preserves_summary_and_active_records(recorded):
    _, _, recorder = recorded
    old = time.time() - RETENTION_SECONDS - 100
    terminal = make_run(recorded, 'terminal', at=old, complete=True)
    active = make_run(recorded, 'active', at=old)
    terminal_ref, _ = recorder.store.put_body(terminal, 'tool', {'result': 'old detail'})
    active_ref, _ = recorder.store.put_body(active, 'tool', {'result': 'active detail'})
    assert recorder.store.expire() == 1
    assert recorder.store.body(terminal, terminal_ref)['state'] == 'expired'
    assert recorder.store.body(active, active_ref)['payload']['result'] == 'active detail'
    assert detail(recorder.store, terminal)['run']['state'] == 'completed'
    assert detail(recorder.store, terminal)['observations']
    assert recorder.store.expire() == 0


def test_body_and_event_limits_are_explicit(recorded, monkeypatch):
    _, _, recorder = recorded
    run = make_run(recorded)
    body_id, status = recorder.store.put_body(run, 'tool', {'text': 'x' * BODY_LIMIT * 2})
    assert status == 'truncated'
    assert recorder.store.body(run, body_id)['payload']['truncated']
    monkeypatch.setattr('chatcopilot.gateway.observation_store.RUN_BODY_LIMIT', 1)
    assert recorder.store.put_body(run, 'tool', 'next') == (None, 'truncated')
    assert detail(recorder.store, run)['run']['capture_state'] == 'truncated'


def test_reader_does_not_write_or_copy_authority(recorded, monkeypatch):
    state, _, recorder = recorded
    run = make_run(recorded, complete=True)
    before = {path.name: path.read_bytes() for path in state.root.iterdir() if path.is_file()}
    index_before = recorder.store.database.read_bytes()
    reader = ObservationStore(state.root)
    monkeypatch.setattr('chatcopilot.gateway.read_model._MAX_SNAPSHOT_BYTES', 1)
    assert detail(reader, run)['run']['state'] == 'completed'
    assert before == {path.name: path.read_bytes() for path in state.root.iterdir() if path.is_file()}
    assert index_before == recorder.store.database.read_bytes()
    with pytest.raises(GatewayStateError):
        reader.set_meta('bad', {})


@pytest.mark.parametrize('unsafe', ['file_mode', 'hardlink', 'symlink'])
def test_body_and_cleanup_reject_unsafe_files(recorded, tmp_path, unsafe):
    _, _, recorder = recorded
    run = make_run(recorded, complete=True)
    body_id, _ = recorder.store.put_body(run, 'tool', {'result': 'keep'})
    path = recorder.store._body_directory(run) / (body_id + '.json')
    if unsafe == 'file_mode':
        path.chmod(0o644)
    elif unsafe == 'hardlink':
        os.link(path, tmp_path / 'outside')
    else:
        path.unlink()
        path.symlink_to(tmp_path / 'outside')
    with pytest.raises((OSError, GatewayStateError)):
        recorder.store.body(run, body_id)
    with pytest.raises((OSError, GatewayStateError)):
        recorder.store.expire(now=time.time() + RETENTION_SECONDS + 100)


def test_diagnostic_failure_does_not_change_task_result(recorded, monkeypatch):
    state, generation, recorder = recorded
    run = make_run(recorded)
    monkeypatch.setattr(recorder, 'reconcile', lambda *_: (_ for _ in ()).throw(OSError('disk')))
    state.finish_run(generation=generation, session_id='session-one', run_id=run,
                     outcome='completed', result={'final_text': 'done'})
    assert state.get_run(run).state == 'completed'


def test_log_capture_only_in_bound_run_scope(recorded, caplog):
    _, _, recorder = recorded
    run = make_run(recorded)
    caplog.set_level(logging.INFO, logger='fixture.execution')
    recorder.start()
    try:
        logger = logging.getLogger('fixture.execution')
        logger.warning('unrelated-record')
        with recorder.scope(run):
            logger.warning('task-record')
    finally:
        recorder.close()
    logs = [item for item in events(recorder.store, run)['observations'] if item['kind'] == 'log']
    assert len(logs) == 1
    assert recorder.store.body(run, logs[0]['body_ref'])['payload']['message'] == 'task-record'


def test_runtime_stage_preserves_boundary_bodies_timing_and_call_binding(recorded, caplog):
    from chatcopilot.core.runtime_observation import runtime_stage
    from chatcopilot.contracts.agent import InputResourceReceipt, InputResourcesDispatched, TurnError
    _, _, recorder = recorded
    run = make_run(recorded)
    observer = RunObserver(recorded[0], recorded[1], run, agent_stage_span_id='host:actor')
    caplog.set_level(logging.INFO, logger='fixture.execution')
    recorder.start()
    try:
        with observer.scope():
            with runtime_stage('channel.receive', 'channel', trace_id=run,
                               input={'text': 'original-body'}, source='channel', target='gateway',
                               occurred_at=123.0, duration_recorded=False) as stage:
                stage.complete({'text': 'canonical-body'})
            with runtime_stage('agent.execute', 'agent', trace_id=run, span_id='host:actor',
                               input={'text': 'execute-input'}) as stage:
                observer(LlmCallStarted('fixture', 1, trace_id=run, span_id='call-1', parent_span_id='host:actor'))
                observer(LlmCallFinished('fixture', 1, trace_id=run, span_id='call-1', parent_span_id='host:actor',
                    visible_response={'content': 'model-visible-body', 'coverage': 'model_response', 'capture_state': 'available'}))
                observer(InputResourcesDispatched('native', 0, 'call-1', (InputResourceReceipt(1, 'image/png', 12, 'a' * 64),)))
                observer(TurnError('fixture_error', 'Agent-visible error'))
                logging.getLogger('fixture.execution').warning('stage-bound-log')
                stage.complete({'final_text': 'agent-visible-body'})
    finally:
        recorder.close()
    rows = events(recorder.store, run)['observations']
    channel = [item for item in rows if item['data'].get('operation') == 'channel.receive']
    assert [item['created_at'] for item in channel] == [123.0, 123.0]
    assert channel[1]['elapsed_ms'] is None
    assert channel[0]['data']['captured_at'] > 123.0
    assert channel[0]['data']['source'] == channel[1]['data']['source'] == 'channel'
    assert recorder.store.body(run, channel[0]['body_ref'])['payload'] == {'input': {'text': 'original-body'}}
    model = next(item for item in rows if item['kind'] == 'LlmCallFinished')
    assert model['data']['stage_span_id'] == 'host:actor'
    assert model['span_id'] == 'call-1'
    assert recorder.store.body(run, model['body_ref'])['payload']['visible_response']['content'] == 'model-visible-body'
    resources = next(item for item in rows if item['kind'] == 'InputResourcesDispatched')
    assert resources['data']['request_id'] == 'call-1'
    assert recorder.store.body(run, resources['body_ref'])['payload']['resources'][0]['sha256'] == 'a' * 64
    log = next(item for item in rows if item['kind'] == 'log')
    assert log['data']['stage_span_id'] == 'host:actor'
    assert log['data']['runtime_layer'] == 'agent'
    error = next(item for item in rows if item['kind'] == 'TurnError')
    assert (error['trace_id'], error['data']['stage_span_id'], error['span_id']) == (run, 'host:actor', None)
    assert all('original-body' not in json.dumps(item) and 'model-visible-body' not in json.dumps(item) for item in rows)


@pytest.mark.parametrize('upstream', ['truncated', 'not_recorded', 'capture_failed'])
def test_upstream_capture_state_and_storage_failure_do_not_hide_each_other(recorded, monkeypatch, upstream):
    _, _, recorder = recorded
    run = make_run(recorded)
    event = {'kind': 'RuntimeStageStarted', 'layer': 'channel', 'data': {}, 'body_state': upstream}
    recorder.store.append(run, event, body={'input': {'capture_state': upstream}})
    first = [row for row in events(recorder.store, run)['observations'] if row['kind'] == 'RuntimeStageStarted'][0]
    assert first['body_state'] == upstream
    assert recorder.store.body(run, first['body_ref'])['state'] == upstream
    monkeypatch.setattr(os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('disk failure')))
    recorder.store.append(run, event, body={'input': {'capture_state': upstream}})
    second = [row for row in events(recorder.store, run)['observations'] if row['kind'] == 'RuntimeStageStarted'][1]
    assert second['body_state'] == 'capture_failed'
    assert second['body_ref'] is None


def test_task_capture_failure_is_not_downgraded_by_later_truncation(recorded):
    _, _, recorder = recorded
    run = make_run(recorded)
    for state in ('capture_failed', 'truncated'):
        recorder.store.append(run, {'kind': 'RuntimeStageStarted', 'layer': 'channel', 'data': {}, 'body_state': state},
                              body={'input': {'capture_state': state}})
    rows = [row for row in events(recorder.store, run)['observations'] if row['kind'] == 'RuntimeStageStarted']
    assert [row['body_state'] for row in rows] == ['capture_failed', 'truncated']
    assert [recorder.store.body(run, row['body_ref'])['state'] for row in rows] == ['capture_failed', 'truncated']
    assert detail(recorder.store, run)['run']['capture_state'] == 'capture_failed'


@pytest.mark.parametrize('failure', ['scope_entry', 'scope_exit', 'record', 'payload'])
def test_runtime_observation_failure_preserves_business_exception_and_context(recorded, monkeypatch, failure):
    from contextlib import contextmanager
    from chatcopilot.core.runtime_observation import current_runtime_stage, result_summary, runtime_stage
    _, _, recorder = recorded
    run = make_run(recorded)
    observer = RunObserver(recorded[0], recorded[1], run)
    if failure in {'scope_entry', 'scope_exit'}:
        @contextmanager
        def broken_scope(_run):
            if failure == 'scope_entry':
                raise OSError('scope entry')
            yield
            raise OSError('scope exit')
        monkeypatch.setattr(recorder, 'scope', broken_scope)
    elif failure == 'record':
        monkeypatch.setattr(recorder, 'record', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('record')))
    with pytest.raises(ValueError, match='business-failure'):
        with observer.scope():
            with runtime_stage('agent.execute', 'agent', trace_id=run, input={'text': 'input'}) as stage:
                if failure == 'payload':
                    assert result_summary(object()) == {'capture_state': 'capture_failed'}
                stage.complete({'text': 'output'})
                raise ValueError('business-failure')
    assert current_runtime_stage() is None


def test_configuration_instances_have_different_entities_and_values():
    one = configuration_projection({'agents': {'backend': 'native'}, 'tools': {'packs': ['workspace.read_write']}})
    two = configuration_projection({'agents': {'backend': 'codex'}, 'tools': {'packs': ['memory.chat']}})
    assert one != two
    assert any(item['id'] == 'pack:memory.chat' for item in two['entities'])
    assert not any(item['id'] == 'pack:memory.chat' for item in one['entities'])


def test_configuration_snapshots_are_immutable(recorded):
    _, _, recorder = recorded
    run = make_run(recorded)
    key = detail(recorder.store, run)['run']['config_id']
    recorder.configuration = {'layers': [], 'entities': [], 'backend': 'codex'}
    recorder.refresh()
    assert recorder.config_id != key
    assert recorder.store.configuration(key)['backend'] == 'native'
    assert detail(recorder.store, run)['run']['config_id'] == key


def test_total_details_above_64_mib_remain_queryable(recorded):
    from chatcopilot.gateway.observation_store import CONTEXT_LIMIT
    _, _, recorder = recorded
    ids = [make_run(recorded, f'large-{index}', complete=True) for index in range(3)]
    payload = {'parts': ['x' * (3 * 1024 * 1024)]}
    refs = []
    for run in ids:
        for _ in range(8):
            ref, state = recorder.store.put_body(run, 'context', payload, limit=CONTEXT_LIMIT)
            assert state == 'available'
            refs.append((run, ref))
    with recorder.store.connection() as connection:
        assert connection.execute('SELECT SUM(body_bytes) FROM runs').fetchone()[0] > 64 * 1024 * 1024
    assert history(recorder.store, RunFilter())['total'] == 3
    assert recorder.store.body(*refs[-1])['payload'] == payload


def test_failed_body_write_cleans_unindexed_file(recorded, monkeypatch):
    _, _, recorder = recorded
    run = make_run(recorded)
    monkeypatch.setattr(os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('fixture disk full')))
    recorder.record(run, {'kind': 'log', 'layer': 'application'}, body={'message': 'unwritten'})
    event = events(recorder.store, run)['observations'][-1]
    assert event['body_state'] == 'capture_failed'
    assert event['body_ref'] is None
    assert list(recorder.store._body_directory(run).iterdir()) == []
    assert detail(recorder.store, run)['run']['capture_state'] == 'capture_failed'


def test_cleanup_validates_all_files_before_deleting_any(recorded, tmp_path):
    _, _, recorder = recorded
    run = make_run(recorded, complete=True)
    first, _ = recorder.store.put_body(run, 'log', {'message': 'retained on unsafe cleanup'})
    second, _ = recorder.store.put_body(run, 'log', {'message': 'unsafe'})
    directory = recorder.store._body_directory(run)
    leaf = directory / (second + '.json')
    leaf.unlink()
    outside = tmp_path / 'outside-body'
    outside.write_text('outside')
    leaf.symlink_to(outside)
    with pytest.raises(GatewayStateError):
        recorder.store.expire(now=time.time() + RETENTION_SECONDS + 100)
    assert (directory / (first + '.json')).is_file()
    assert outside.read_text() == 'outside'


def test_execution_authorization_retains_exact_trace_and_separates_visibility(recorded):
    from chatcopilot.application.tool_authorization import build_tool_permission_filter
    from chatcopilot.agent.trace import TraceContext, set_trace, reset_trace
    from chatcopilot.core.observation_context import permission_phase
    from chatcopilot.contracts.authorization import Principal, stable_payload_digest
    from chatcopilot.contracts.identity import ConversationIdentity, Role
    from chatcopilot.contracts.tools import ToolDef
    state, generation, recorder = recorded
    run = make_run(recorded)
    observer = RunObserver(state, generation, run)
    principal = Principal(channel='fixture', account_id='account', user_id='actor',
                          conversation=ConversationIdentity('fixture', 'p2p', 'chat'), role=Role.USER,
                          evidence_digest=stable_payload_digest({'event': 'fixture'}))
    check = build_tool_permission_filter(principal, policy_version="v1")
    tool = ToolDef(
        "search_public",
        "search",
        {},
        {},
        handler=lambda *_: None,
        category="agent.search",
        access="member",
    )
    with recorder.scope(run):
        assert check(tool) is None
    token = set_trace(TraceContext('trace', 'tool-call', 0, observer))
    try:
        # Relay threads carry the trace sink even when the Gateway context is absent.
        with permission_phase('execution'):
            assert check(tool) is None
    finally:
        reset_trace(token)
    checks = [item for item in events(recorder.store, run)['observations'] if item['kind'] == 'tool_authorization']
    assert [item['data']['phase'] for item in checks] == ['visibility', 'execution']
    assert checks[1]['span_id'] == 'tool-call' and checks[1]['trace_id'] == 'trace'


def test_background_link_uses_task_id_from_host_tool_result(recorded):
    state, generation, recorder = recorded
    run = make_run(recorded)
    RunObserver(state, generation, run)(ToolFinished('start_code_task', True, 'accepted',
        data={'ok': True, 'data': {'task_id': 'code-job-fixture', 'status': 'queued'}}, trace_id='trace', span_id='start'))
    event = events(recorder.store, run)['observations'][-1]
    assert event['data']['related_task_id'] == 'code-job-fixture'
    assert event['data']['relation_capture'] == 'summary_only'


def test_loaded_instances_use_effective_models_registry_and_plugin_health(tmp_path):
    from types import SimpleNamespace
    from chatcopilot.agent.runtime import build_agent_runtime
    from chatcopilot.botspec.loader import load_botspec
    from chatcopilot.core.config import ChatConfig, LLMConfig
    from chatcopilot.gateway.observation_runtime import runtime_configuration
    projections = []
    for index, pack in enumerate(('workspace.read_write', 'memory.chat')):
        folder = tmp_path / str(index)
        folder.mkdir()
        (folder / 'identity.md').write_text('fixture identity')
        spec_path = folder / 'bot.yaml'
        spec_path.write_text(f'id: fixture-{index}\nprompts:\n  schema_version: 2\n  identity: identity.md\nagents:\n  backend: native\ntools:\n  packs: [{pack}]\n')
        spec = load_botspec(spec_path)
        agent = build_agent_runtime(chat_config=ChatConfig(llm=LLMConfig(api_key='fixture', model=f'model-{index}')),
                                    tool_packs=(pack,), agent_backend='native')
        try:
            agent.mcp_provider = SimpleNamespace(status=lambda: [{'id': 'fixture-plugin', 'running': False,
                'tools_count': 0, 'error': 'fixture_timeout'}])
            runtime = SimpleNamespace(spec=spec, mcp_servers=[{'id': 'fixture-plugin', 'enabled': True}], skills=[], rag_sources=[], prompt_profile={})
            config = runtime_configuration(runtime, agent, {})
            projections.append(config)
            entities = {item['id']: item for item in config['entities']}
            assert entities['model-slot:chat']['runtime']['model'] == f'model-{index}'
            assert entities['mcp:fixture-plugin']['connected'] is False
            assert entities['mcp:fixture-plugin']['loaded'] is False
            assert entities['mcp:fixture-plugin']['runtime']['error'] == 'fixture_timeout'
            assert entities['pack:' + pack]['loaded'] is True
            assert config['tool_bindings']
        finally:
            agent.mcp_provider = None
            agent.close()
    assert projections[0]['tool_bindings'] != projections[1]['tool_bindings']
