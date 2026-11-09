from pathlib import Path
import json
import subprocess
from uuid import uuid4

import pytest

from studio.applications import ApplicationManager


class Runner:
    def __init__(self):
        self.active = 'inactive'
        self.calls = []
    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'systemd-run':
            self.active = 'active'
        if args[:3] == ['systemctl', '--user', 'stop']:
            self.active = 'inactive'
        return subprocess.CompletedProcess(args, 0, self.active, '')


@pytest.fixture
def app(tmp_path):
    runner = Runner()
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    manager = ApplicationManager(home, Path(__file__).resolve().parents[2], runner=runner)
    return manager, uuid4().hex, runner


def model(manager):
    from chatcopilot.core.install_artifacts import write_private
    write_private(manager.state_dir / 'models.json', json.dumps({
        'connections': {'local': {'kind': 'openai_compatible', 'base_url': 'http://localhost:8000/v1',
                                 'auth': {'mode': 'api_key', 'key_env': 'TEST_KEY'}}},
        'profiles': {'app': {'connection': 'local', 'model': 'test'}},
        'bindings': {'chat': 'app'}}))


def test_configuration_is_private_and_partial_updates_preserve_secrets(app):
    manager, project, runner = app
    assert manager.get(project)['state'] == 'not_created'
    secret = 'synthetic-secret-for-test'
    view = manager.configure(project, {'name':'助手', 'instructions':'简短回答', 'app_id':'1234', 'app_secret':secret})
    assert secret not in json.dumps(view)
    root = manager.state_dir / 'applications' / project
    assert secret not in (root / 'application.json').read_text()
    assert secret not in (root / 'bot.yaml').read_text()
    assert (root / 'credentials.env').stat().st_mode & 0o777 == 0o600
    manager.configure(project, {'instructions':'更简短'})
    assert manager.get(project)['app_id'] == '1234'
    assert secret in (root / 'credentials.env').read_text()
    assert not manager.check(project)['ready']
    model(manager)
    result = manager.check(project)
    assert result['ready'], result
    assert result['scope'] == 'local_configuration'
    assert result['end_to_end_verified'] is False


def test_start_stop_idempotent_no_worker_and_no_secret_arguments(app):
    manager, project, runner = app
    model(manager)
    manager.configure(project, {'app_id':'1234', 'app_secret':'synthetic-secret'})
    assert manager.start(project)['state'] == 'running_unverified'
    manager.start(project)
    assert sum(c[0] == 'systemd-run' for c in runner.calls) == 1
    assert 'synthetic-secret' not in str(runner.calls)
    with pytest.raises(ValueError, match='停止'):
        manager.configure(project, {'name':'别的名字'})
    # A new Studio process sees the same real unit and saved application.
    restored = ApplicationManager(manager.state_dir, manager.framework, runner=runner)
    assert restored.get(project)['state'] == 'running_unverified'
    restored.shutdown()
    assert runner.active == 'active'
    assert restored.stop(project)['state'] == 'stopped'


def test_bad_ids_fields_and_secret_injection(app):
    manager, project, _ = app
    with pytest.raises(ValueError):
        manager.get('../outside')
    for data in ({'extra':'x'}, {'app_id':'invalid'}, {'app_secret':'bad\nINJECT=1'}, {'instructions':1}):
        with pytest.raises(ValueError):
            manager.configure(project, data)
    manager.configure(project, {'app_id':'1234', 'app_secret':'synthetic-secret'})
    with pytest.raises(ValueError, match='AppSecret'):
        manager.configure(project, {'app_id':'5678'})


def test_official_botspec_and_host_identity(app):
    manager, project, _ = app
    manager.configure(project, {'app_id':'1234', 'app_secret':'synthetic-secret'})
    from chatcopilot.botspec.runtime import load_runtime_context
    from chatcopilot.host.channel_assembly import assemble_channel
    from chatcopilot.channels.qq_official import QQOfficialChannelConfig
    context = load_runtime_context(manager.state_dir / 'applications' / project / 'bot.yaml')
    assert context.platform_type == 'qq_official'
    assembly = assemble_channel(QQOfficialChannelConfig('qq_official','1234','synthetic-secret'),
                                state_root=manager.state_dir, policy_version='test', qq_users='*')
    assert assembly.owners == ()
    assert assembly.account.channel == 'qq_official'
    assert assembly.admission_policy.qq_official_account == '1234'


def test_official_admission_never_uses_numeric_qq_or_display_names():
    from chatcopilot.authorization.policy import AdmissionPolicy, IdentityPolicy
    from chatcopilot.contracts.identity import ConversationIdentity, TurnIdentity, Identity, Role
    from chatcopilot.contracts.authorization import AuthorizationRequest, AuthorizationOperation
    turn = TurnIdentity(ConversationIdentity("qq_official", "p2p", "opaque-user"), "opaque-user", "owner")
    principal = IdentityPolicy(owners=(Identity(name="owner"),)).principal(
        turn=turn, channel="qq_official", account_id="1234", evidence_digest="evidence")
    assert principal.role == Role.USER
    request = AuthorizationRequest(request_id="test", principal=principal,
        operation=AuthorizationOperation.INGRESS, target="conversation", params_digest="payload")
    default = AdmissionPolicy.from_raw(qq_users="*", policy_version="test")
    assert not default.decide(request).allowed
    from dataclasses import replace
    assert replace(default, qq_official_account="1234").decide(request).allowed
    assert not replace(default, qq_official_account="5678").decide(request).allowed


def test_private_manifest_and_credentials_reject_symlinks(app, tmp_path):
    manager, project, _ = app
    root = manager._root(project)
    target = tmp_path / "elsewhere"
    target.write_text("do not change")
    (root / "credentials.env").symlink_to(target)
    from chatcopilot.core.install_artifacts import InstallError
    with pytest.raises(InstallError):
        manager.configure(project, {"app_id":"1234", "app_secret":"synthetic-secret"})
    assert target.read_text() == "do not change"

def test_generated_runtime_resolves_official_channel_without_worker(app):
    manager, project, _ = app
    manager.configure(project, {'app_id':'1234', 'app_secret':'synthetic-secret'})
    from chatcopilot.botspec.runtime import load_runtime_context
    from chatcopilot.host.runtime import parse_gateway_runtime_config
    from chatcopilot.core.private_sqlite import private_directory
    root = manager._root(project)
    context = load_runtime_context(root / 'bot.yaml')
    config = parse_gateway_runtime_config(context, {
        'AGENTSTRATA_QQ_APP_ID':'1234', 'AGENTSTRATA_QQ_APP_SECRET':'synthetic-secret',
        'CHATCOPILOT_GATEWAY_TOKEN':'g' * 48, 'CHATCOPILOT_GATEWAY_PORT':'18765',
        'CHATCOPILOT_GATEWAY_STATE_ROOT':str(private_directory(root / 'gateway')),
        'CHATCOPILOT_WORKSPACE_ROOT':str(private_directory(root / 'workspace'))})
    assert config.channel.app_id == '1234'
    assert 'synthetic-secret' not in repr(config)


def test_channel_failure_is_distinct_from_process_and_no_raw_errors(app, monkeypatch):
    manager, project, runner = app
    manager.configure(project, {"app_id": "1234", "app_secret": "synthetic-secret"})
    runner.active = "active"
    async def rejected(*args):
        return "starting", "qq_official_api_rejected"
    monkeypatch.setattr(manager, "_query_channel", rejected)
    view = manager.get(project)
    assert view["process_state"] == "active"
    assert view["channel"]["error_code"] == "qq_official_api_rejected"
    assert not view["channel"]["connected"]
    assert "AppSecret" in view["message"]
    assert not view["end_to_end_verified"]
    async def malicious(*args):
        return "failed", "synthetic-secret"
    monkeypatch.setattr(manager, "_query_channel", malicious)
    assert "synthetic-secret" not in json.dumps(manager.get(project))
    async def connected(*args):
        return "connected", None
    monkeypatch.setattr(manager, "_query_channel", connected)
    view = manager.get(project)
    assert view["channel"]["connected"]
    assert not view["end_to_end_verified"]


def test_official_secret_field_is_explicit(app):
    manager, project, _ = app
    manager.configure(project, {"app_id": "1234", "app_secret": "synthetic-secret"})
    from chatcopilot.botspec.runtime import load_runtime_context
    context = load_runtime_context(manager._root(project) / "bot.yaml")
    # Public type names AppSecret, not the short-lived access token obtained by the driver.
    from chatcopilot.botspec.model import QQOfficialChannelSpec
    assert QQOfficialChannelSpec().app_secret_env == "AGENTSTRATA_QQ_APP_SECRET"
    assert "access_token_env" not in QQOfficialChannelSpec.__dataclass_fields__
    assert context.platform_type == "qq_official"
