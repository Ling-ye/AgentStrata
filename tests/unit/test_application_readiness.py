"""External Studio definitions cannot masquerade as source-registered applications."""
import json
import shutil
from uuid import uuid4

import pytest

from console.control.applications import Applications
from console.control.application_readiness import improvement_readiness
from console.control.instances import BotInstance
from chatcopilot.evals.application.bots import EvaluationBotRef, EvaluationBotResolver, bot_spec_path
from chatcopilot.core.application_installation import installation, RECORD, SCHEMA
from chatcopilot.core.install_artifacts import write_private
from studio.applications import ApplicationManager
from tests.unit.test_console_applications import client_for


@pytest.fixture
def source_app(tmp_path, monkeypatch):
    root = tmp_path / 'repository'
    root.mkdir()
    monkeypatch.setattr(ApplicationManager, '_active', lambda self, project: 'inactive')
    apps = Applications(root, tmp_path / 'studio', discover=lambda: [])
    row = apps.create(uuid4().hex, 'qq_official')
    apps.configure(row['id'], {'name': 'test', 'instructions': 'brief', 'app_id': '123',
                               'app_secret': 'SYNTHETIC-PRIVATE-VALUE'})
    return apps, row, apps.manager()._root(row['project_id'])


def test_studio_readiness_is_precise_read_only_and_does_not_register(source_app):
    apps, row, root = source_app
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    result = improvement_readiness(apps, row['id'])
    assert not result['source_supported'] and not result['execution_verified']
    assert {c['code'] for c in result['checks'] if c['status'] == 'blocked'} == {
        'external_definition_not_registered', 'application_source_baseline_missing', 'running_source_evidence_missing'}
    assert len(result['requirements']) == 4
    assert 'SYNTHETIC-PRIVATE-VALUE' not in json.dumps(result)
    assert str(root) not in json.dumps(result)
    assert before == {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert not (apps.root / '.git').exists()
    with pytest.raises(KeyError):
        EvaluationBotResolver(apps.root)('qq-app-' + row['project_id'])


def test_external_botref_and_symlink_cannot_bypass_source_containment(source_app):
    apps, row, root = source_app
    spec = root / 'bot.yaml'
    ref = EvaluationBotRef('qq-app-' + row['project_id'], spec, root)
    with pytest.raises(ValueError, match='escapes repository'):
        bot_spec_path(ref, apps.root)
    (apps.root / 'bots').mkdir()
    (apps.root / 'bots/alias').symlink_to(root, target_is_directory=True)
    with pytest.raises(KeyError):
        EvaluationBotResolver(apps.root)(ref.instance_id)


def test_forged_installation_record_cannot_register_a_studio_directory(source_app):
    apps, row, root = source_app
    write_private(root / RECORD, json.dumps({'schema': SCHEMA, 'bot_id': 'qq-app-' + row['project_id'],
                                             'bot_relative': 'bot.yaml', 'delivered_digest': 'a' * 64}))
    with pytest.raises(ValueError, match='identity'):
        installation(root)
    assert not improvement_readiness(apps, row['id'])['source_supported']
    with pytest.raises(KeyError):
        EvaluationBotResolver(apps.root)('qq-app-' + row['project_id'])


def test_repository_source_requires_exact_identity_not_just_same_id(source_app):
    apps, row, root = source_app
    target = apps.root / 'bots/registered'
    target.mkdir(parents=True)
    for name in ('bot.yaml', 'agent.yaml', 'identity.md', 'response.md'):
        shutil.copyfile(root / name, target / name)
    ident = 'qq-app-' + row['project_id']
    apps._discover = lambda: [BotInstance(ident, str(target / 'bot.yaml'))]
    result = improvement_readiness(apps, 'repo:' + ident)
    assert result['source_supported'] and not result['execution_verified']
    # A discovery alias outside the repository cannot inherit the registered id's authority.
    apps._discover = lambda: [BotInstance(ident, str(root / 'agent.yaml'))]
    result = improvement_readiness(apps, 'repo:' + ident)
    assert not result['source_supported']
    assert result['checks'][0]['code'] == 'source_registration_unavailable'


def test_readiness_api_is_local_scoped_and_has_no_execution_action(source_app):
    apps, row, _ = source_app
    route = f'/api/applications/{row["id"]}/improvement-readiness'
    with client_for(apps) as client:
        assert client.get(route).json()['scope'] == 'source_registration'
        assert client.post(route).status_code == 405
        assert client.get('/api/applications/studio:missing/improvement-readiness').status_code == 404
        assert client.get(route, headers={'Origin': 'https://external.invalid'}).status_code == 403
    with client_for(apps, '192.0.2.1') as client:
        assert client.get(route).status_code == 403
