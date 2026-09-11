from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from chatcopilot.botspec.inspection import configuration_projection, declared_configuration, expected_configuration
from chatcopilot.contracts.agent import ContextSnapshotPrepared, ToolFinished, ToolStarted
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.contracts.identity import Role
from chatcopilot.core.inspection import plain
from chatcopilot.core.observability_redaction import bound_observability_payload, redact_observability_payload
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway.observations import RunObserver
from chatcopilot.gateway.state_store import GatewayStateStore
from console.backend.app import app
from console.backend.routes import architecture
from console.control.instances import BotInstance


def entity(config, identity):
    return next(item for item in config['entities'] if item['id'] == identity)


def test_operator_projection_preserves_lists_empty_values_references_and_paths(tmp_path):
    numbers = ','.join(('100' + '200301', '100' + '200302'))
    private = 'fixture-' + 'credential-value'
    values = {'CHATCOPILOT_ADMINS': numbers, 'QQ_ALLOW_FROM': '*', 'CHATCOPILOT_OWNERS': '',
              'FIXTURE_TOKEN': private, 'FIXTURE_CHAT_API_KEY': private,
              'FIXTURE_CHAT_MODEL': 'configured-model', 'UNRELATED_ENV': 'not-part-of-instance'}
    config = configuration_projection({'gateway': {'token_env': 'FIXTURE_TOKEN'},
        'channels': {'qq': {'account_env': 'UNSET_ACCOUNT'}}, 'llm': {'chat': {'env_prefix': 'FIXTURE_CHAT'}}},
        mcp=[{'id': 'lookup', 'env': {'TOKEN': '${FIXTURE_TOKEN}'}}], environment=values)
    policy = entity(config, 'policy:instance')['config']
    assert policy['CHATCOPILOT_ADMINS'] == numbers
    assert policy['QQ_ALLOW_FROM'] == '*'
    assert policy['CHATCOPILOT_OWNERS'] == ''
    assert entity(config, 'channel:qq')['environment'] == {'UNSET_ACCOUNT': None}
    assert entity(config, 'gateway:instance')['environment'] == {'FIXTURE_TOKEN': private}
    assert entity(config, 'mcp:lookup')['environment'] == {'FIXTURE_TOKEN': private}
    assert entity(config, 'model-slot:chat')['environment'] == {
        'FIXTURE_CHAT_API_KEY': private, 'FIXTURE_CHAT_MODEL': 'configured-model'}
    assert 'not-part-of-instance' not in json.dumps(config)
    assert plain(tmp_path / 'model') == str(tmp_path / 'model')
    assert config['visibility'] == 'operator'


def test_bounded_operator_copy_keeps_original_keys_but_public_redaction_still_masks(tmp_path):
    private = 'fixture-' + 'private-value'
    payload = {'api_key': private, str(tmp_path): {'Authorization': 'Bearer ' + private},
               'text': 'token=' + private, 'path': tmp_path / 'source'}
    copied = bound_observability_payload(payload)
    assert copied.value['api_key'] == private
    assert copied.value[str(tmp_path)]['Authorization'] == 'Bearer ' + private
    assert copied.value['path'] == str(tmp_path / 'source')
    assert copied.replacement_count == 0 and not copied.truncated
    assert private not in json.dumps(redact_observability_payload(payload, secrets=(private,)).value)
    cycle = {}
    cycle['self'] = cycle
    assert bound_observability_payload(cycle).truncation_reasons == ('cycle',)
    deep = []
    for _ in range(70):
        deep = [deep]
    assert 'depth_limit' in bound_observability_payload(deep).truncation_reasons


def test_operator_values_survive_recording_api_refresh_and_instance_boundaries(tmp_path, monkeypatch):
    # Keep snapshot freshness independent of host wall-clock corrections.
    wall_start, monotonic_start = time.time(), time.monotonic()
    clock = SimpleNamespace(time=lambda: wall_start + time.monotonic() - monotonic_start)
    monkeypatch.setattr('chatcopilot.gateway.observation_runtime.time', clock)
    monkeypatch.setattr('console.control.gateway_observability.time', clock)
    instances, recorders, states, environments = {}, [], [], []
    for key in ('QQ_ALLOW_FROM', 'CHATCOPILOT_OWNERS', 'CHATCOPILOT_ADMINS'):
        monkeypatch.delenv(key, raising=False)
    for index in range(2):
        folder = tmp_path / str(index)
        folder.mkdir(mode=0o700)
        (folder / 'identity.md').write_text('Fixture identity')
        spec = folder / 'bot.yaml'
        spec.write_text(f'id: fixture-{index}\nprompts:\n  schema_version: 2\n  identity: identity.md\n'
            'gateway: {}\nchannels:\n  qq:\n    type: qq_personal\n    provider: onebot_v11\n'
            'llm:\n  chat:\n    env_prefix: FIXTURE_CHAT\nagents:\n  backend: native\n')
        state = GatewayStateStore(folder / 'gateway')
        generation = state.acquire_writer_generation()
        values = {'CHATCOPILOT_GATEWAY_STATE_ROOT': str(state.root),
                  'CHATCOPILOT_ADMINS': '100' + f'20030{index}', 'QQ_ALLOW_FROM': '100' + f'40050{index}',
                  'QQ_ACCESS_TOKEN': 'fixture-' + f'credential-{index}', 'FIXTURE_CHAT_MODEL': f'model-{index}',
                  'FIXTURE_CHAT_API_KEY': 'fixture-' + f'llm-value-{index}'}
        env = folder / 'local.env'
        env.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        env.chmod(0o600)
        environments.append(values)
        current = expected_configuration(spec, values, home=Path.home())
        loaded = declared_configuration(spec, values)
        loaded["environment_revision"] = current["effective_environment_revision"]
        recorder = ObservationRecorder(state, generation, configuration=loaded)
        recorders.append(recorder)
        states.append(state)
        instance = BotInstance(f'fixture-{index}', str(spec), env_file=str(env), runtime_kind='gateway')
        instances[instance.instance_id] = instance
        state.create_session(generation=generation, session_id='session', account=ChannelAccountRef('fixture', 'account'),
                             conversation=ConversationRef('p2p', 'fixture'))
        state.begin_run(generation=generation, session_id='session', run_id='run-values', input_fingerprint='a' * 64)
        observer = RunObserver(state, generation, 'run-values')
        observer.prepare(SimpleNamespace(principal=SimpleNamespace(role=Role.OWNER), canonical_text=values['CHATCOPILOT_ADMINS']))
        observer(ToolStarted('lookup', {'user_id': values['QQ_ALLOW_FROM'], 'token': values['QQ_ACCESS_TOKEN']},
                             trace_id='run-values', span_id='lookup'))
        observer(ToolFinished('lookup', True, values['CHATCOPILOT_ADMINS'],
                             data={'path': str(folder)}, trace_id='run-values', span_id='lookup'))
        observer(ContextSnapshotPrepared('ctx-values', 'native', 'fixture', 1,
            ({'role': 'assistant', 'content': 'answer', 'reasoning_content': 'excluded-provider-thought'},),
            ({'role': 'user', 'content': 'api_key=' + values['FIXTURE_CHAT_API_KEY']},)))
        recorder.start()
        try:
            with observer.scope():
                logging.getLogger('fixture.operator').warning('token=%s', values['QQ_ACCESS_TOKEN'])
        finally:
            recorder.close()
        state.start_run(generation=generation, session_id='session', run_id='run-values')
        state.finish_run(generation=generation, session_id='session', run_id='run-values', outcome='completed',
                         result={'final_text': values['QQ_ALLOW_FROM']})
    monkeypatch.setattr(architecture, 'get_instance', lambda identity: instances[identity])
    with TestClient(app) as client:
        for recorder in recorders:
            recorder.refresh()
        base = '/api/bots/fixture-0'
        current = client.get(base + '/inspection')
        assert current.headers['cache-control'] == 'no-store'
        assert entity(current.json()['current'], 'policy:instance')['config']['CHATCOPILOT_ADMINS'] == environments[0]['CHATCOPILOT_ADMINS']
        assert entity(current.json()['current'], 'channel:qq')['environment']['QQ_ACCESS_TOKEN'] == environments[0]['QQ_ACCESS_TOKEN']
        assert not current.json()['pending_changes']
        detail = client.get(base + '/gateway-observation/runs/run-values').json()
        payloads = []
        for reference in [detail['run']['input_ref'], detail['run']['result_ref'],
                          *(event['body_ref'] for event in detail['observations'] if event['body_ref'])]:
            route = '/gateway-observation/runs/run-values/details/' + reference
            response = client.get(base + route)
            assert response.headers['cache-control'] == 'no-store'
            payloads.append(response.json()['payload'])
            assert client.get('/api/bots/fixture-1' + route).status_code == 404
            assert client.get(base + route.replace('run-values', 'run-other')).status_code == 404
        assert payloads[0]['text'] == environments[0]['CHATCOPILOT_ADMINS']
        assert payloads[1]['final_text'] == environments[0]['QQ_ALLOW_FROM']
        assert next(item for item in payloads if 'arguments' in item)['arguments']['token'] == environments[0]['QQ_ACCESS_TOKEN']
        assert next(item for item in payloads if 'level' in item)['message'] == 'token=' + environments[0]['QQ_ACCESS_TOKEN']
        context = next(item for item in payloads if 'effective_messages' in item)
        assert context['effective_messages'][0]['content'] == 'api_key=' + environments[0]['FIXTURE_CHAT_API_KEY']
        assert 'excluded-provider-thought' not in json.dumps(payloads)
        assert str(tmp_path / '0') in json.dumps(payloads)
        before = client.get(base + '/inspection?run_id=run-values').json()['execution']
        env = Path(instances['fixture-0'].env_file)
        env.write_text(env.read_text().replace(environments[0]['CHATCOPILOT_ADMINS'], '*'))
        recorders[0].refresh()
        after = client.get(base + '/inspection?run_id=run-values').json()
        assert after['pending_changes'], after['configuration_status_reason']
        assert entity(after['current'], 'policy:instance')['config']['CHATCOPILOT_ADMINS'] == '*'
        assert after['execution'] == before
        deployed_env = tmp_path / '0' / 'runtime.env'
        deployed_env.write_text(''.join(f'{key}={value}\n' for key, value in environments[0].items()))
        deployed_env.chmod(0o600)
        instances['fixture-0'].env_file = str(deployed_env)
        env.write_text(env.read_text().replace('CHATCOPILOT_ADMINS=*\n', ''))
        removed = client.get(base + '/inspection').json()
        assert entity(removed['current'], 'policy:instance')['config']['CHATCOPILOT_ADMINS'] is None
        monkeypatch.setenv('CHATCOPILOT_ADMINS', '100' + '999001')
        overridden = client.get(base + '/inspection').json()
        assert entity(overridden['current'], 'policy:instance')['config']['CHATCOPILOT_ADMINS'] is None
        assert client.get('/api/not-found').headers['cache-control'] == 'no-store'
        deployed_env.chmod(0o644)
        unsafe = client.get(base + '/inspection')
        assert unsafe.status_code == 409
        assert environments[0]['QQ_ACCESS_TOKEN'] not in unsafe.text
