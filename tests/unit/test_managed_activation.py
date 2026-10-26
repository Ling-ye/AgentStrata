"""Version transactions with controlled service facts; no deployed instance is touched."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.release_identity import configuration_manifest, environment_identity, model_identity, tree_manifest
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.plugins.activation_repository import ActivationRepository
from chatcopilot.plugins.activation_service import ActivationService
from chatcopilot.plugins.activation_types import ActivationTarget, ReleaseSource
from chatcopilot.plugins.types import ServiceDefinition
from tests.application_fixture import write_application


def manifest(root):
    return {str(p.relative_to(root)): {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "executable": False}
            for p in root.rglob("*") if p.is_file()}


class ProcessDied(BaseException):
    pass


class Runtime:
    def __init__(self, target, source, environment):
        self.target, self.source, self.environment = target, source, environment
        self.running = True
        self.stops = self.starts = 0
        self.pending = 0
        self.fail_candidate = False
        self.fail_restore = False
        self.die = ""
        self.identity = "original-target"
        self._before = self._base()

    def _base(self):
        return {"gateway": {"version": None, "components": {"chatcopilot": digest(tree_manifest(self.source.baseline / "src/chatcopilot"))},
            "configuration": digest(configuration_manifest(self.source.baseline / self.source.bot_relative)),
            "model": self.source.model_identity, "environment": self.source.runtime_environment,
            "gate_root": str(self.target.state_root), "bot": str(self.source.baseline / self.source.bot_relative),
            "loaded": True}}

    def installation_lock(self):
        return nullcontext()

    def fingerprint(self):
        return self.identity

    def observe(self):
        if not self.running:
            raise RuntimeError("not active")
        pointer = ActivationRepository(self.target.control).pointer()
        if pointer:
            return {"gateway": {**pointer["services"]["gateway"], "bot": str(Path(pointer["source_root"]) / pointer["bot_relative"]), "loaded": True}}
        return deepcopy(self._before)

    def check_environment(self, observed):
        assert observed["gateway"]["environment"] == self.source.runtime_environment

    def compile(self, *args):
        return {"check": "fixture compilation", "passed": True}

    def pending_work(self):
        return self.pending

    def stop(self):
        self.stops += 1
        self.running = False

    def require_stopped(self):
        assert not self.running

    def start(self):
        self.starts += 1
        candidate = ActivationRepository(self.target.control).pointer() is not None
        if self.die == "before-start" and candidate:
            self.die = ""
            raise ProcessDied()
        if (candidate and self.fail_candidate) or (not candidate and self.fail_restore):
            raise RuntimeError("controlled startup failure")
        self.running = True
        if self.die == "after-start" and candidate:
            self.die = ""
            raise ProcessDied()


@pytest.fixture
def release(tmp_path, monkeypatch):
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    for root, value in ((baseline, 0), (candidate, 1)):
        (root / "src/chatcopilot").mkdir(parents=True)
        (root / "src/chatcopilot/probe.py").write_text(f"VALUE = {value}\n")
        (root / "bots/sample").mkdir(parents=True)
        write_application(root / "bots/sample/bot.yaml", {"id": "sample", "display_name": "Sample",
            "agents": {"runtime": "langgraph"}, "llm": {"chat": {"binding": "chat"}}})
    models = tmp_path / "models.json"
    models.write_text(json.dumps({"connections": {"fixture": {"kind": "openai_compatible", "auth": {"mode": "api_key", "key_env": "RELEASE_KEY"}}},
        "profiles": {"fixture": {"connection": "fixture", "model": "fixture-model"}}, "bindings": {"chat": "fixture"}}))
    monkeypatch.setenv("AGENTSTRATA_LLM_CONFIG", str(models))
    monkeypatch.setenv("RELEASE_KEY", "synthetic-version-test-key")
    root = tmp_path / "installed"
    root.mkdir(mode=0o700)
    state = root / "state"
    state.mkdir(mode=0o700)
    (state / "memory.txt").write_text("user state must survive")
    env = root / "runtime.env"
    env.write_text("AGENTSTRATA_LLM_CONFIG=" + str(models) + "\n")
    env.chmod(0o600)
    target = ActivationTarget("instance:sample", root, baseline / "bots/sample/bot.yaml", env, state,
        (ServiceDefinition("gateway", (sys.executable, "-m", "fixture"), str(root), str(env), 18888),),
        {"chatcopilot": "src/chatcopilot"}, {"gateway": "fixture.service"})
    before, after = manifest(baseline), manifest(candidate)
    source = ReleaseSource("repair-" + "a" * 32, "sample", baseline, before, candidate, after,
        {"candidate_digest": manifest_digest(after), "goal_digest": "goal", "verification_digest": "verification", "review_binding": "review"},
        "bots/sample/bot.yaml", environment_identity(), model_identity(baseline / "bots/sample/bot.yaml", dict(__import__("os").environ)))
    runtime = Runtime(target, source, dict(__import__("os").environ))
    return ActivationService(runtime, timeout=.15), source, runtime


def test_prepare_is_non_disruptive_then_activate_and_withdraw_preserve_data(release):
    service, source, runtime = release
    record = service.prepare(source)
    assert record["state"] == "prepared" and runtime.stops == runtime.starts == 0
    assert service.prepare(source)["id"] == record["id"]
    active = service.activate(record["id"])
    assert active["state"] == "active" and service.status()["running_matches"] is True
    assert runtime.stops == runtime.starts == 1
    assert service.activate(record["id"])["state"] == "active"
    withdrawn = service.rollback(record["id"])
    assert withdrawn["state"] == "rolled_back" and withdrawn["id"] != record["id"]
    assert service.repository.pointer() is None and runtime.running
    assert (runtime.target.state_root / "memory.txt").read_text() == "user state must survive"


def test_failed_start_restores_prior_version_and_retains_failed_candidate(release):
    service, source, runtime = release
    record = service.prepare(source)
    runtime.fail_candidate = True
    result = service.activate(record["id"])
    assert result["state"] == "rolled_back" and result["restored"] is True
    assert runtime.running and service.repository.pointer() is None
    assert Path(record["candidate_root"]).is_dir()


def test_failed_restoration_is_not_reported_as_success(release):
    service, source, runtime = release
    record = service.prepare(source)
    runtime.fail_candidate = runtime.fail_restore = True
    assert service.activate(record["id"])["state"] == "recovery_required"
    assert not runtime.running
    runtime.fail_restore = False
    assert service.reconcile(record["id"])["state"] == "rolled_back"


def test_reconciliation_honors_a_recorded_restore_intent_even_if_candidate_is_live(release):
    service, source, runtime = release
    record = service.prepare(source)
    service.activate(record["id"])
    service._save(record, "recovery_required", restore_required=True)
    result = service.reconcile(record["id"])
    assert result["state"] == "rolled_back"
    assert service.repository.pointer() is None


def test_failed_withdrawal_can_be_retried_without_losing_its_history(release):
    service, source, runtime = release
    record = service.prepare(source)
    service.activate(record["id"])
    runtime.fail_restore = True
    failed = service.rollback(record["id"])
    assert failed["state"] == "blocked" and failed["restored"]
    runtime.fail_restore = False
    succeeded = service.rollback(failed["id"])
    assert succeeded["state"] == "rolled_back" and succeeded["id"] != failed["id"]
    assert (service.repository.root / "history" / (failed["id"] + ".json")).exists()


def test_withdrawal_remains_available_after_original_goal_is_cancelled(release):
    service, source, _ = release
    record = service.prepare(source)
    service.activate(record["id"])
    service.authorized = lambda: False
    assert service.rollback(record["id"])["state"] == "rolled_back"


def test_withdrawal_can_retry_after_waiting_for_active_work_times_out(release):
    service, source, runtime = release
    record = service.prepare(source)
    service.activate(record["id"])
    runtime.pending = 1
    blocked = service.rollback(record["id"])
    assert blocked["state"] == "blocked" and service.repository.pointer() is not None
    runtime.pending = 0
    assert service.rollback(blocked["id"])["state"] == "rolled_back"


def test_credential_rotation_does_not_change_the_versioned_model_reference():
    from dataclasses import replace
    from chatcopilot.core.model_config import LLMConfig
    from chatcopilot.core.release_identity import selected_model_identity
    before = LLMConfig(model="fixture", api_key="old-private-key", key_env="SAME_REFERENCE")
    assert selected_model_identity("langgraph", before) == selected_model_identity("langgraph", replace(before, api_key="rotated-private-key"))
    assert selected_model_identity("langgraph", before) != selected_model_identity("langgraph", replace(before, model="different"))


@pytest.mark.parametrize("crash, expected, starts", [("before-start", "rolled_back", 2), ("after-start", "active", 1)])
def test_interrupted_activation_reconciles_observed_version_without_replaying_start(release, crash, expected, starts):
    service, source, runtime = release
    record = service.prepare(source)
    runtime.die = crash
    with pytest.raises(ProcessDied):
        service.activate(record["id"])
    assert service.repository.read()["state"] == "starting"
    with pytest.raises(ValueError, match="interrupted"):
        service.prepare(source)
    assert service.reconcile(record["id"])["state"] == expected
    assert runtime.starts == starts


def test_pending_work_does_not_stop_or_change_the_instance(release):
    service, source, runtime = release
    record = service.prepare(source)
    runtime.pending = 1
    result = service.activate(record["id"])
    assert result["state"] == "blocked" and runtime.stops == 0
    assert service.repository.pointer() is None and runtime.running


def test_configuration_or_registration_drift_blocks_before_stopping(release):
    service, source, runtime = release
    record = service.prepare(source)
    runtime.identity = "changed-target"
    with pytest.raises(ValueError, match="registration"):
        service.activate(record["id"])
    assert runtime.stops == 0


def test_queries_are_read_only_and_prepared_cancellation_never_starts_services(release):
    service, source, runtime = release
    record = service.prepare(source)
    path = service.repository.root / "operation.json"
    before = path.read_bytes()
    service.status(record["id"])
    assert path.read_bytes() == before
    assert service.cancel(record["id"])["state"] == "cancelled"
    assert runtime.stops == runtime.starts == 0


def test_modified_prepared_artifact_is_rejected_before_stopping(release):
    service, source, runtime = release
    record = service.prepare(source)
    (Path(record["candidate_root"]) / "src/chatcopilot/probe.py").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        service.activate(record["id"])
    assert runtime.stops == runtime.starts == 0


def test_release_controls_remain_hidden_inside_an_owner_project(tmp_path, monkeypatch):
    from chatcopilot.application.execution_scope import execution_scope
    from chatcopilot.contracts.identity import Role
    control = tmp_path / "installation/.agentstrata-versions"
    gateway = tmp_path / "installation/private/gateway"
    monkeypatch.setenv("AGENTSTRATA_RELEASE_CONTROL", str(control))
    monkeypatch.setenv("AGENTSTRATA_RELEASE_GATE_ROOT", str(gateway))
    scope = execution_scope(Role.OWNER, tmp_path / "workspace", (tmp_path,))
    assert scope.permits(tmp_path / "ordinary.py", write=True)
    assert not scope.permits(control / "current.json", write=True)
    assert not scope.permits(gateway / "release-admission.lock", write=True)


def test_avatar_action_is_not_dispatched_during_the_switch_window(tmp_path, monkeypatch):
    import asyncio
    from chatcopilot.core.release_gate import effects_idle
    from chatcopilot.evals.bridge_cases import live2d_module
    from chatcopilot.protocols.gateway_client import GatewayClientConfig
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("AGENTSTRATA_RELEASE_GATE_ROOT", str(state))
    config = live2d_module("config")
    adapter = config.AdapterConfig(GatewayClientConfig("ws://127.0.0.1:1", "t" * 48, client_id="avatar"),
        "c" * 48, "fixture", {}, {"greet": config.Motion("TapBody", 0)}, ("http://127.0.0.1",))
    controller = live2d_module("avatar").BrowserAvatarController(adapter)
    sent = []
    async def send(message):
        payload = json.loads(message)
        sent.append(payload)
        controller.acknowledge("browser", {"request_id": payload["request_id"], "status": "executed"})
    controller.attach("browser", "fixture", send)
    async def run():
        with effects_idle(state, 1):
            result = await controller.play_motion("greet")
            assert result["status"] == "unavailable" and not sent
        assert (await controller.play_motion("greet"))["status"] == "executed"
        assert len(sent) == 1
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1, 0, True])
def test_activation_deadline_must_be_finite(release, timeout):
    _, _, runtime = release
    with pytest.raises(ValueError):
        ActivationService(runtime, timeout=timeout)


@pytest.mark.parametrize("path", ["pyproject.toml", "src/chatcopilot/core/private_store.py", "unowned.py"])
def test_unvalidated_build_data_or_component_changes_are_rejected(release, path):
    from dataclasses import replace
    service, source, runtime = release
    extra = source.candidate / path
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("changed")
    updated = manifest(source.candidate)
    with pytest.raises(ValueError, match="separate recovery|outside the selected"):
        service.prepare(replace(source, candidate_manifest=updated,
                                receipt={**source.receipt, "candidate_digest": manifest_digest(updated)}))
    assert runtime.stops == 0
