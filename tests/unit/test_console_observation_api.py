from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatcopilot.botspec.inspection import declared_configuration
from chatcopilot.contracts.gateway import ChannelAccountRef, ConversationRef
from chatcopilot.gateway.observation_runtime import ObservationRecorder
from chatcopilot.gateway.state_store import GatewayStateStore
from console.backend.routes import architecture
from console.control.instances import BotInstance


def test_observation_http_scope_filter_snapshot_and_no_store(tmp_path, monkeypatch):
    instances = {}
    stores = []
    for index in range(2):
        folder = tmp_path / str(index)
        folder.mkdir(mode=0o700)
        (folder / 'identity.md').write_text('A configured fixture bot')
        (folder / 'style.md').write_text('Respond clearly')
        spec = folder / 'bot.yaml'
        spec.write_text(f'id: fixture-{index}\nprompts:\n  schema_version: 2\n  identity: identity.md\n  response_style: style.md\nagents:\n  backend: native\ngateway: {{}}\nchannels:\n  qq:\n    type: qq_personal\n    provider: onebot_v11\n')
        state = GatewayStateStore(folder / 'gateway')
        generation = state.acquire_writer_generation()
        config = declared_configuration(spec, {})
        recorder = ObservationRecorder(state, generation, configuration=config)
        stores.append(recorder.store)
        env = folder / 'local.env'
        env.write_text('CHATCOPILOT_GATEWAY_STATE_ROOT=' + str(state.root) + '\n')
        env.chmod(0o600)
        inst = BotInstance('fixture-' + str(index), str(spec), env_file=str(env), runtime_kind='gateway')
        instances[inst.instance_id] = inst
        state.create_session(generation=generation, session_id='session', account=ChannelAccountRef('fixture', 'account'),
                             conversation=ConversationRef('p2p', 'chat'))
        state.begin_run(generation=generation, session_id='session', run_id='run-fixture', input_fingerprint=hashlib.sha256(b'fixture').hexdigest())
        recorder.record('run-fixture', {'kind': 'fixture', 'layer': 'agent', 'entity_id': 'agent:main'}, body={'message': f'instance-{index}'})
    monkeypatch.setattr(architecture, 'get_instance', lambda identity: instances[identity])
    app = FastAPI()
    app.include_router(architecture.router)
    client = TestClient(app)
    base = '/api/bots/fixture-0/gateway-observation'
    overview = client.get(base + '?search=fixture&limit=1')
    assert overview.status_code == 200 and overview.headers['cache-control'] == 'no-store'
    assert overview.json()['total'] == 1
    detail = client.get(base + '/runs/run-fixture').json()
    event = detail['observations'][-1]
    reference = event['body_ref']
    assert 'instance-0' not in json.dumps(detail)
    body = client.get(base + '/runs/run-fixture/details/' + reference)
    assert body.headers['cache-control'] == 'no-store' and body.json()['payload']['message'] == 'instance-0'
    crossed = client.get('/api/bots/fixture-1/gateway-observation/runs/run-fixture/details/' + reference)
    assert crossed.status_code == 404 and crossed.headers['cache-control'] == 'no-store'
    assert client.get(base + '?page=2').json()['runs'] == []
    assert client.get(base + '?since=nan').status_code == 400
    assert client.get(base + '?model=' + 'x' * 257).status_code == 400
    assert client.get(base + '/runs/run-fixture/events?after=' + str(event['seq'])).json()['observations'] == []
    before = client.get('/api/bots/fixture-0/inspection?run_id=run-fixture&event_seq=' + str(event['seq'])).json()
    assert before['execution']['configuration_revision'] == before['current']['configuration_revision']
    (tmp_path / '0' / 'style.md').write_text('A different response style')
    after = client.get('/api/bots/fixture-0/inspection?run_id=run-fixture').json()
    assert after['pending_changes']
    assert after['execution']['configuration_revision'] == before['execution']['configuration_revision']
    assert client.get('/api/bots/fixture-0/inspection?run_id=run-fixture&event_seq=9999').status_code == 400
    database = stores[0].database
    database.chmod(0o644)
    unsafe = client.get(base)
    assert unsafe.status_code == 409 and unsafe.headers['cache-control'] == 'no-store'
