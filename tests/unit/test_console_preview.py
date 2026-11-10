"""No-network tests of the app-scoped preview proxy and its durable retry boundary."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from console.control.applications import Applications
from console.control.application_preview import Preview, CLIENT
from chatcopilot.contracts.gateway_rpc import TextRpcSegment
from chatcopilot.protocols.gateway_client import GatewayRemoteError, GatewayMutationOutcomeUnknown
from chatcopilot.contracts.gateway_rpc import client_run_id
from chatcopilot.core.install_artifacts import write_private
from studio.applications import ApplicationManager
from tests.unit.test_console_applications import client_for


@pytest.fixture
def preview(tmp_path, monkeypatch):
    apps = Applications(Path(__file__).resolve().parents[2], tmp_path / 'state', discover=lambda: [])
    monkeypatch.setattr(ApplicationManager, '_active', lambda self, project: 'inactive')
    rows = [apps.create(uuid4().hex, 'qq_official') for _ in range(2)]
    for row in rows:
        apps.configure(row['id'], {'name': 'test'})
        write_private(apps.manager()._root(row['project_id']) / 'runtime.env',
                      'CHATCOPILOT_GATEWAY_PORT="18765"\nAGENTSTRATA_PREVIEW_TOKEN="' + 'p' * 48 + '"\n')
    monkeypatch.setattr(ApplicationManager, '_active', lambda self, project: 'active')
    state = SimpleNamespace(calls=[], runs={}, sessions=set(), lose_response=False, refuse=False)
    class Client:
        def __init__(self, config):
            assert config.client_id == CLIENT and config.client_mode == 'interactive'
            assert config.token == 'p' * 48
            assert config.scopes == ('gateway.read', 'chat.write', 'chat.abort')
        async def connect(self):
            if state.refuse:
                raise OSError('private-token-must-not-escape')
        async def close(self): pass
        async def request(self, method, params, **kwargs):
            state.calls.append((method, params, kwargs))
            assert method in {'runs.get', 'sessions.get', 'sessions.create', 'chat.send', 'chat.abort'}
            if method == 'sessions.get':
                if params.session_id not in state.sessions:
                    raise GatewayRemoteError('session_not_found', 'missing')
                return SimpleNamespace()
            if method == 'sessions.create':
                state.sessions.add(params.session_id)
                return SimpleNamespace()
            if method == 'runs.get':
                if params.run_id not in state.runs:
                    raise GatewayRemoteError('run_not_found', 'missing')
                return SimpleNamespace(run=state.runs[params.run_id])
            if method == 'chat.send':
                run = client_run_id(client_id=CLIENT, session_id=params.session_id,
                                     idempotency_key=kwargs['idempotency_key'])
                state.runs[run] = SimpleNamespace(state='running', segments=(), error_code=None)
                if state.lose_response:
                    raise GatewayMutationOutcomeUnknown('unknown', 'private detail')
                return SimpleNamespace(run_id=run, session_id=params.session_id)
            if method == 'chat.abort':
                state.runs[params.run_id].state = 'aborted'
                return SimpleNamespace(aborted=True)
    monkeypatch.setattr('console.control.application_preview.GatewayWebSocketClient', Client)
    return Preview(apps), rows, state


def test_unknown_send_queries_original_run_without_resending(preview):
    api, rows, state = preview
    row = rows[0]
    key = uuid4().hex
    state.lose_response = True
    first = api.operate(row['id'], 'send', key, 'hello')
    assert first['messages'][0]['state'] == 'unknown'
    assert 'private detail' not in json.dumps(first)
    again = api.operate(row['id'], 'retry', key)
    assert again['messages'][0]['state'] == 'running'
    assert sum(c[0] == 'chat.send' for c in state.calls) == 1
    state.runs[next(iter(state.runs))].state = 'completed'
    state.runs[next(iter(state.runs))].segments = (TextRpcSegment('response'),)
    restored = Preview(Applications(api.apps.root, api.apps.state_dir, discover=lambda: []))
    final = restored.operate(row['id'])
    assert final['messages'][0]['reply'] == 'response'
    assert api.apps.messages(row['id']) == []  # Never a management conversation.


def test_cross_app_id_session_and_payload_isolation(preview):
    api, (a, b), state = preview
    key = uuid4().hex
    api.operate(a['id'], 'send', key, 'hello')
    with pytest.raises(ValueError):
        api.operate(b['id'], 'send', key, 'hello')
    with pytest.raises(ValueError):
        api.operate(a['id'], 'send', key, 'changed')
    with pytest.raises(KeyError):
        api.operate(b['id'], 'abort', key)
    assert api.operate(b['id'])['messages'] == []
    api.operate(b['id'], 'send', uuid4().hex, 'other')
    assert len(state.sessions) == 2
    assert all(s.startswith('console-preview-') for s in state.sessions)


def test_read_never_retries_unknown_mutation_and_abort_uses_original_run(preview):
    api, rows, state = preview
    row = rows[0]
    key = uuid4().hex
    state.refuse = True
    api.operate(row['id'], 'send', key, 'hello')
    state.refuse = False
    api.operate(row['id'])
    assert not any(c[0] == 'chat.send' for c in state.calls)
    api.operate(row['id'], 'retry', key)
    api.operate(row['id'], 'abort', key)
    assert api.operate(row['id'])['messages'][0]['state'] == 'aborted'
    assert sum(c[0] == 'chat.send' for c in state.calls) == 1


def test_running_legacy_process_requires_explicit_restart(preview, monkeypatch):
    api, rows, state = preview
    row = rows[0]
    root = api.apps.manager()._root(row['project_id'])
    write_private(root / 'runtime.env', 'CHATCOPILOT_GATEWAY_PORT="18765"\nCHATCOPILOT_GATEWAY_TOKEN="' + 'g' * 48 + '"\n')
    result = api.operate(row['id'])
    assert not result['available'] and '停止后重新启动' in result['reason']
    assert not api.apps.detail(row['id'])['capabilities']['chat']
    assert not state.calls
    with pytest.raises(ValueError):
        api.operate(row['id'], 'send', uuid4().hex, 'hello')
    monkeypatch.setattr(ApplicationManager, '_active', lambda self, project: 'inactive')
    assert not api.operate(row['id'])['available']


def test_preview_api_rejects_session_url_credentials_and_remote(preview):
    api, rows, _ = preview
    row = rows[0]
    with client_for(api.apps) as client:
        url = f'/api/applications/{row["id"]}/chat'
        response = client.post(url, json={'id': uuid4().hex, 'text': 'hello', 'token': 'DO-NOT-ECHO',
                                         'session_id': 'qq-native', 'url': 'http://external.invalid'})
        assert response.status_code == 422 and 'DO-NOT-ECHO' not in response.text
        response = client.post(url, json={'id': uuid4().hex, 'text': 'hello'})
        assert response.status_code == 200 and response.json()['messages'][0]['state'] == 'accepted'
    with client_for(api.apps, '192.0.2.1') as client:
        assert client.get(url).status_code == 403


def test_public_run_identity_preserves_v1_algorithm():
    import hashlib
    from chatcopilot.gateway.coordinator import _client_run_id as coordinator_id
    from chatcopilot.gateway.dispatcher import _client_run_id as dispatcher_id
    values = dict(client_id="console-preview", session_id="app-fixture", idempotency_key="request-fixture")
    expected = "run_" + hashlib.sha256(b"console-preview\0app-fixture\0request-fixture").hexdigest()[:32]
    assert client_run_id(**values) == coordinator_id(**values) == dispatcher_id(**values) == expected
