"""Independent applications retain the existing source, repair and M5 boundaries."""
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chatcopilot.core.application_installation import (
    RECORD, SCHEMA, application_identity, application_source, installation, running_application_identity,
)
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest
from chatcopilot.harness.api import HarnessController
from chatcopilot.harness.control_types import WorkerState
from chatcopilot.harness.improvement_repository import baseline, initialize
from chatcopilot.harness.models import HarnessError, RepairOptions
from chatcopilot.harness.models import VerificationCheck, VerificationResult
from chatcopilot.harness.repair_runtime import run_task
from chatcopilot.contracts.deployment import ReleaseSource
from chatcopilot.deployment.application_bundle import export_bundle
from chatcopilot.deployment.application_runtime import install_bundle, setup_application
from test_harness_repair_v2 import Coder, Verifier, repair as repair_fixture
from tests.application_fixture import write_application


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    private_directory(path.parent)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def installed(tmp_path):
    store, task_id, root = repair_fixture.__wrapped__(tmp_path)
    (root / "bots/fixture").mkdir(parents=True)
    write_application(root / "bots/fixture/bot.yaml", {
        "id": "fixture", "display_name": "Independent fixture", "agents": {"runtime": "langgraph"},
        "llm": {"chat": {"binding": "bot.fixture.chat"}, "research": {"binding": "bot.fixture.research"}},
        "gateway": {"protocol_version": 1, "clients": [{"id": "test", "token_env": "TEST_TOKEN", "role": "owner"}]},
    })
    private_json(root / RECORD, {"schema": SCHEMA, "bot_id": "fixture",
        "bot_relative": "bots/fixture/bot.yaml", "delivered_digest": "a" * 64})
    return root, store, task_id


def select_candidate(root, candidate):
    manifest = {str(path.relative_to(candidate)): {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "executable": False,
    } for path in candidate.rglob("*") if path.is_file()}
    ident = manifest_digest(manifest)
    directory = root / ".agentstrata-versions/versions" / ident
    copy_sources(candidate, directory / "source", manifest)
    private_json(directory / "manifest.json", manifest)
    private_json(root / ".agentstrata-versions/current.json", {
        "version": ident, "source_root": str(directory / "source"), "bot_relative": "bots/fixture/bot.yaml",
    })
    return directory / "source", manifest


def test_installation_is_explicit_and_private_metadata_is_not_candidate_source(installed):
    root, _store, _task = installed
    assert installation(root).bot_id == "fixture"
    selected, manifest = application_source(root, "fixture")
    assert selected == root
    assert RECORD not in manifest
    with pytest.raises(ValueError, match="does not belong"):
        application_source(root, "another")
    record = root / RECORD
    record.unlink()
    assert installation(root) is None
    record.symlink_to(root / "bots/fixture/bot.yaml")
    with pytest.raises((ValueError, OSError)):
        installation(root)


def test_active_version_becomes_next_local_baseline_without_changing_installation(installed, tmp_path):
    root, store, task_id = installed
    original = application_identity(root)
    candidate = tmp_path / "candidate"
    copy_sources(root, candidate, source_manifest(root))
    (candidate / "src/chatcopilot/core/probe.py").write_text("VALUE = 2\n")
    selected, manifest = select_candidate(root, candidate)
    state = baseline(root)
    assert state["source_root"] == str(selected)
    assert state["source_digest"] == manifest_digest(manifest) != original["source_digest"]
    store.update(task_id, delivery=state, source={"kind": "robot_task", "bot_id": "fixture"})
    initialize(store, task_id)
    frozen = store.root / "jobs" / task_id / "source"
    assert (frozen / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 2\n"
    assert (root / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 0\n"
    assert running_application_identity(selected, root / ".agentstrata-versions") == {
        "bot_id": "fixture", "source_digest": state["source_digest"],
    }
    # A process that still loaded the base source cannot claim the newer pointer.
    assert running_application_identity(root, root / ".agentstrata-versions")["source_digest"] == original["source_digest"]


def test_local_robot_feedback_requires_its_actual_application_version(installed, monkeypatch):
    root, _store, _task = installed
    identity = application_identity(root)
    recorded = {key: identity[key] for key in ("bot_id", "source_digest")}
    source = {"kind": "robot_task", "bot_id": "fixture", "run_id": "run-fixture", "revision": "fixture", "original_input": "Return one",
        "evidence": {"configuration": {"application_version": recorded}}, "failure_signature": [], "blockers": []}
    monkeypatch.setattr("chatcopilot.harness.delivery.remote_baseline", lambda *_: pytest.fail("Independent application consulted GitHub"))
    controller = HarnessController(root, root=root.parent / "feedback-control",
        evaluator=SimpleNamespace(), task_reader=lambda *_: source,
        worker_control=SimpleNamespace(observe=lambda _: WorkerState.INACTIVE))
    task = controller.start_task("fixture", "run-fixture", RepairOptions("fixture"), launch=False)
    saved = controller.store.get(task["task_id"])
    assert saved["delivery"]["mode"] == "local"
    assert saved["source"]["application_version"]["source_digest"] == identity["source_digest"]
    source["evidence"]["configuration"]["application_version"] = {**recorded, "source_digest": "b" * 64}
    with pytest.raises(HarnessError, match="任务记录缺少当前应用版本"):
        controller.start_task("fixture", "run-fixture", RepairOptions("fixture"), launch=False)


@pytest.fixture
def application_bundle(installed, tmp_path):
    root, _store, _task = installed
    files = {"pyproject.toml": '[project]\nname = "fixture"\nversion = "1.0.0"\n', "uv.lock": "version = 1\n",
        "src/chatcopilot/__main__.py": "# Synthetic installation entry\n", "src/chatcopilot/application_cli.py": "# Synthetic CLI\n",
        "deploy/wsl/install_wsl_env.sh": "#!/bin/sh\nexit 0\n", "deploy/wsl/update_instance.sh": "#!/bin/sh\nexit 0\n"}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    manifest = source_manifest(root)
    receipt = {"candidate_digest": manifest_digest(manifest), "goal_digest": "goal",
               "verification_digest": "verification", "review_binding": "review", "attempt": 1}
    source = ReleaseSource("fixture-task", "fixture", root, manifest, root, manifest, receipt,
                           "bots/fixture/bot.yaml", {"fixture": "runtime"}, "fixture-model")
    bundle = tmp_path / "delivery"
    export_bundle(source, bundle)
    return bundle, root


def test_bundle_installs_a_self_contained_copy_and_preserves_agent_profile(application_bundle, tmp_path):
    bundle, original = application_bundle
    expected_profile = (original / "bots/fixture/agent.yaml").read_bytes()
    expected_application = (original / "bots/fixture/bot.yaml").read_bytes()
    destination = tmp_path / "receiver"
    result = install_bundle(bundle, destination)
    assert result["state"] == "installed" and not result["reused"]
    assert (destination / "bots/fixture/agent.yaml").read_bytes() == expected_profile
    assert (destination / "bots/fixture/bot.yaml").read_bytes() == expected_application
    assert installation(destination).deployment_overrides()["wsl_home"] == str(destination)
    assert subprocess.check_output(["git", "-C", str(destination), "rev-parse", "--absolute-git-dir"], text=True).strip() == str(destination / ".git")
    data = destination / ".agentstrata-data/workspace/memory.txt"
    data.parent.mkdir(parents=True)
    data.write_text("user-owned conversation")
    (destination / "bots/fixture/local.env").write_text("TOKEN=synthetic-secret\n")
    (destination / "runtime.env").write_text("TOKEN=synthetic-secret\n")
    assert install_bundle(bundle, destination)["reused"]
    assert data.read_text() == "user-owned conversation"
    manifest = source_manifest(destination)
    assert not any(".agentstrata-data" in name or name.endswith(".env") for name in manifest)
    shutil.rmtree(original)
    shutil.rmtree(bundle)
    assert application_identity(destination)["bot_id"] == "fixture"
    assert manifest == source_manifest(destination)


@pytest.mark.parametrize("change", ["content", "extra", "symlink", "receipt"])
def test_changed_bundle_is_rejected_before_creating_the_installation(application_bundle, tmp_path, change):
    bundle, _original = application_bundle
    target = bundle / "source/src/chatcopilot/core/probe.py"
    if change == "content":
        target.write_text("VALUE = 99\n")
    elif change == "extra":
        (bundle / "source/unrecorded.py").write_text("VALUE = 99\n")
    elif change == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "outside.py")
    else:
        record = json.loads((bundle / "application.json").read_text())
        record["accepted_candidate"]["candidate_digest"] = "f" * 64
        (bundle / "application.json").write_text(json.dumps(record))
    with pytest.raises((ValueError, OSError)):
        install_bundle(bundle, tmp_path / "receiver")
    assert not (tmp_path / "receiver").exists()


def test_existing_directory_is_never_overwritten(application_bundle, tmp_path):
    bundle, _original = application_bundle
    destination = tmp_path / "user-directory"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("owned by the user")
    with pytest.raises(ValueError, match="Destination already exists"):
        install_bundle(bundle, destination)
    assert sentinel.read_text() == "owned by the user"


def test_installation_paths_are_private_host_configuration(application_bundle, tmp_path):
    from chatcopilot.botspec.loader import load_botspec
    from chatcopilot.botspec.deployment_env import deployment_environment
    bundle, _original = application_bundle
    root = tmp_path / "receiver"
    install_bundle(bundle, root)
    bot = root / "bots/fixture/bot.yaml"
    original = bot.read_bytes()
    values = deployment_environment(load_botspec(bot), {}, source_root=root, home=tmp_path / "account")
    assert values["CHATCOPILOT_HOME"] == str(root)
    assert values["CHATCOPILOT_ENV_FILE"] == str(root / ".agentstrata-versions/runtime.env")
    assert values["CHATCOPILOT_WORKSPACE_ROOT"] == str(root / ".agentstrata-data/workspace")
    assert bot.read_bytes() == original
    script = root / "deploy/wsl/_load_env.sh"
    script.write_bytes((Path(__file__).resolve().parents[2] / "deploy/wsl/_load_env.sh").read_bytes())
    environment = {key: value for key, value in os.environ.items() if not key.startswith("CHATCOPILOT_")}
    environment.update(CHATCOPILOT_BOT_SPEC=str(bot), CHATCOPILOT_INSTANCE_ID="fixture",
        AGENTSTRATA_DEPLOY_PYTHON=sys.executable)
    result = subprocess.run(["bash", "-c",
        'source "$1"; ccp_apply_bot_deploy_config; printf "%s\\n%s\\n" "$CHATCOPILOT_HOME" "$CHATCOPILOT_ENV_FILE"',
        "fixture", str(script)], env=environment, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [str(root), str(root / ".agentstrata-versions/runtime.env")]


def test_setup_does_not_replace_another_installations_service(application_bundle, tmp_path, monkeypatch):
    bundle, _original = application_bundle
    root = tmp_path / "receiver"
    install_bundle(bundle, root)
    account = tmp_path / "account"
    registration = account / ".config/chatcopilot-console/fixture.env"
    private_directory(registration.parent)
    registration.write_text("CCP_WSL_HOME=/another/installation\n")
    registration.chmod(0o600)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Foreign service was touched"))
    with pytest.raises(ValueError, match="Another installation owns"):
        setup_application(root)


def test_existing_export_requires_the_same_acceptance(application_bundle):
    bundle, root = application_bundle
    record = json.loads((bundle / "application.json").read_text())
    manifest = source_manifest(root)
    source = ReleaseSource("new-task", "fixture", root, manifest, root, manifest,
        {**record["accepted_candidate"], "goal_digest": "another-goal"}, "bots/fixture/bot.yaml",
        record["runtime_environment"], record["model_identity"])
    with pytest.raises(ValueError, match="another application delivery"):
        export_bundle(source, bundle)


def test_console_and_eval_resolve_the_installed_application_and_active_source(application_bundle, tmp_path, monkeypatch):
    from console.control.discovery import discover_instances, repo_root
    from chatcopilot.evals.application.bots import EvaluationBotResolver, bot_relative_path
    from chatcopilot.evals.code_source import prepare_code_source
    bundle, _original = application_bundle
    root = tmp_path / "receiver"
    install_bundle(bundle, root)
    monkeypatch.setenv("CHATCOPILOT_SOURCE_ROOT", str(root))
    repo_root.cache_clear()
    try:
        assert repo_root() == root
        assert [item.instance_id for item in discover_instances()] == ["fixture"]
        assert discover_instances()[0].wsl_home == str(root)
        candidate = tmp_path / "next-version"
        copy_sources(root, candidate, source_manifest(root))
        (candidate / "src/chatcopilot/core/probe.py").write_text("VALUE = 2\n")
        selected, manifest = select_candidate(root, candidate)
        bot = EvaluationBotResolver(root)("fixture")
        assert bot.source_root == selected
        assert bot.bot_spec == selected / "bots/fixture/bot.yaml"
        assert str(bot_relative_path(bot, root)) == "bots/fixture/bot.yaml"
        with pytest.raises(KeyError):
            EvaluationBotResolver(root)("other")
        receipt = prepare_code_source(root, {"path": str(selected), "sha256": manifest_digest(manifest)}, tmp_path / "eval-copy")
        assert (Path(receipt["path"]) / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 2\n"
        assert (Path(receipt["path"]) / "bots/fixture/agent.yaml").is_file()
    finally:
        repo_root.cache_clear()


class VersionedRepairVerifier(Verifier):
    def prepare(self, *args):
        source, plan = super().prepare(*args)
        return source, replace(plan, real_agent=True, repetitions=3, check_repetitions={"target": 3})

    def run(self, task, candidate, ident, checks, cancel):
        passed = "VALUE = 1" in (candidate.path / "src/chatcopilot/core/probe.py").read_text()
        return VerificationResult(ident, candidate.digest, tuple(
            VerificationCheck("target", number, "passed" if passed else "failed", "" if passed else "product")
            for number in (1, 2, 3)), (ident,), {ident: {"snapshot": {
                "source_sha256": candidate.digest, "runtime": {"fixture": "same"},
                "verification_sha256": "a" * 64}, "model_identity": "b" * 64}})


def test_local_feedback_uses_existing_acceptance_and_can_prepare_a_release(installed, monkeypatch):
    from chatcopilot.harness.activation_runtime import release_source
    root, _old_store, _task = installed
    identity = application_identity(root)
    source = {"kind": "robot_task", "bot_id": "fixture", "run_id": "run-fixture", "revision": "fixture",
        "original_input": "Return one", "failure_signature": [], "blockers": [],
        "evidence": {"configuration": {"application_version": {key: identity[key] for key in ("bot_id", "source_digest")}}}}
    monkeypatch.setattr("chatcopilot.harness.delivery.remote_baseline", lambda *_: pytest.fail("GitHub was consulted"))
    controller = HarnessController(root, root=root.parent / "feedback-control", evaluator=SimpleNamespace(),
        task_reader=lambda *_: source, worker_control=SimpleNamespace(observe=lambda _: WorkerState.INACTIVE))
    task = controller.start_task("fixture", "run-fixture", RepairOptions("fixture"), launch=False)
    completed = run_task(controller.store, task["task_id"], VersionedRepairVerifier(), Coder())
    assert completed["status"] == "fixed", completed.get("message")
    assert completed["delivery"]["mode"] == "local"
    released = release_source(controller, task["task_id"])
    assert released.bot_id == "fixture"
    assert (released.candidate / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 1\n"
    assert (root / "src/chatcopilot/core/probe.py").read_text() == "VALUE = 0\n"
