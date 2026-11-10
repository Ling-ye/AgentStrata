"""Synthetic local source and installation tests; no model or QQ requests."""
import json
from types import SimpleNamespace

import pytest

from tests.unit.test_studio_applications import app as application_fixture, model
from chatcopilot.core.application_installation import installation
from chatcopilot.core.source_snapshot import source_manifest, manifest_digest
from chatcopilot.evals.application.bots import EvaluationBotResolver, bot_spec_path
from studio import qq_sources


@pytest.fixture
def app(tmp_path):
    return application_fixture.__wrapped__(tmp_path)


def prepared(app):
    manager, project, runner = app
    manager.configure(project, {"app_id": "1234", "app_secret": "synthetic-source-secret",
                                "instructions": "Brief replies"})
    result = manager.prepare_source(project)
    source, record = qq_sources.resolve(manager, project)
    return manager, project, runner, source, record, result


def test_prepare_is_private_complete_resolvable_and_not_accepted(app, monkeypatch):
    manager, project, runner, source, record, result = prepared(app)
    assert result["state"] == "prepared"
    assert installation(source) is None
    assert manager.prepare_source(project) == result
    manifest = source_manifest(source)
    assert "src/chatcopilot/__main__.py" in manifest
    assert "deploy/wsl/update_instance.sh" in manifest
    import tomllib
    metadata = tomllib.loads((source / "pyproject.toml").read_text())
    assert metadata["project"]["readme"] in manifest
    assert (source / metadata["project"]["readme"]).is_file()
    assert not any(n.endswith(".env") or "source-binding" in n for n in manifest)
    assert not any("synthetic-source-secret" in (source / n).read_text(errors="ignore") for n in manifest)
    selected = EvaluationBotResolver(source)("qq-app-" + project)
    assert bot_spec_path(selected, source) == source / "bots" / ("qq-app-" + project) / "bot.yaml"
    from chatcopilot.evals.service.server import EvaluationServiceRuntime
    model(manager)
    monkeypatch.setenv("AGENTSTRATA_LLM_CONFIG", str(manager.model_path))
    monkeypatch.setenv("TEST_KEY", "synthetic-key")
    service = EvaluationServiceRuntime(repository_root=source,
        artifact_root=manager._root(project) / "test-control", verification_root=manager.framework)
    preview = service.dispatch("evaluations.preview", {"bot_id": "qq-app-" + project,
        "request": {"kind": "suite", "suite_id": "agentstrata-agent-tasks-v1",
                    "preset": "custom", "case_ids": ["decision-no-tool"], "repetitions": 1}})
    assert preview["targets"], preview
    assert preview["ready"], preview
    # Preparation does not activate candidate code or prevent ordinary standard operation.
    model(manager)
    manager.start(project)
    launch = next(c for c in runner.calls if c[0] == "systemd-run")
    assert "--working-directory=" + str(manager.framework) in launch
    assert manager.source_status(project)["execution_verified"] is False


def test_definition_or_source_changes_fail_closed(app):
    manager, project, _, source, _, _ = prepared(app)
    (source / "bots" / ("qq-app-" + project) / "identity.md").write_text("changed")
    assert manager.source_status(project)["state"] == "stale"
    with pytest.raises(ValueError):
        manager.source_controller(project)
    manager.configure(project, {"instructions": "New definition"})
    assert manager.source_status(project)["state"] == "stale"
    assert manager.prepare_source(project)["state"] == "prepared"


def test_scoped_controller_uses_explicit_model_and_separate_state(app, monkeypatch):
    manager, project, _, source, record, _ = prepared(app)
    calls = []
    monkeypatch.setattr("studio.runtime.controller", lambda *args, **kwargs: calls.append((args, kwargs)) or "host")
    assert manager.source_controller(project) == "host"
    args, kwargs = calls[0]
    assert args[0] == source
    assert args[1] == manager._root(project) / "development" / record["digest"]
    assert kwargs["environment"] == manager.model_environment
    assert kwargs["start_eval"] is False
    assert (args[1] / "binding.json").exists()


def test_install_requires_real_export_acceptance_and_keeps_current_source(app, monkeypatch):
    manager, project, _, source, record, _ = prepared(app)
    class Rejected:
        repository = source
        def get(self, task_id):
            return {"source": {"bot_id": "qq-app-" + project}}
        def export_delivery(self, task_id, bundle):
            raise ValueError("independent acceptance missing")
    monkeypatch.setattr(qq_sources, "controller", lambda *a, **kw: Rejected())
    with pytest.raises(ValueError, match="acceptance"):
        manager.install_delivery(project, "repair-" + "a" * 32)
    assert qq_sources.resolve(manager, project) == (source, record)
    assert not (manager._root(project) / "installations").exists()


def test_install_and_runtime_use_exact_exported_source(app, monkeypatch):
    # Controlled exporter fixture tests installation plumbing, not actual acceptance.
    from chatcopilot.contracts.deployment import ReleaseSource
    from chatcopilot.deployment.application_bundle import export_bundle
    manager, project, runner, source, record, _ = prepared(app)
    manifest = source_manifest(source)
    digest = manifest_digest(manifest)
    receipt = {"candidate_digest": digest, "goal_digest": "synthetic-goal",
               "verification_digest": "synthetic-verification", "review_binding": "synthetic-review"}
    relative = "bots/qq-app-" + project + "/bot.yaml"
    release = ReleaseSource("synthetic-task", "qq-app-" + project, source, manifest, source,
                            manifest, receipt, relative, {"fixture": "runtime"}, "synthetic-model")
    host = SimpleNamespace(repository=source, get=lambda _: {"source": {"bot_id": "qq-app-" + project}},
                           export_delivery=lambda _, target: export_bundle(release, target))
    monkeypatch.setattr(qq_sources, "controller", lambda *a, **kw: host)
    result = manager.install_delivery(project, "repair-" + "a" * 32)
    installed, binding = qq_sources.resolve(manager, project)
    assert result["state"] == "installed"
    assert installation(installed).bot_id == "qq-app-" + project
    assert manager.install_delivery(project, "repair-" + "a" * 32) == result
    model(manager)
    manager.start(project)
    launch = next(c for c in runner.calls if c[0] == "systemd-run")
    assert "--working-directory=" + str(installed) in launch
    assert str(installed / relative) in launch
    env = (manager._root(project) / "runtime.env").read_text()
    assert str(installed / "src") in env
    runtime = json.loads((manager._root(project) / "runtime-binding.json").read_text())
    assert runtime["source_digest"] == digest
    assert "synthetic-source-secret" not in str(launch)


def test_invalid_or_dangling_source_binding_never_falls_back_to_standard_source(app):
    manager, project, _ = app
    manager.configure(project, {"name": "private app"})
    root = manager._root(project)
    binding = root / "source-binding.json"
    from chatcopilot.core.install_artifacts import write_private
    write_private(binding, "[]")
    assert manager.source_status(project)["state"] == "stale"
    with pytest.raises(ValueError):
        qq_sources.resolve(manager, project)
    binding.unlink()
    binding.symlink_to(root / "missing-binding.json")
    with pytest.raises((ValueError, OSError)):
        qq_sources.resolve(manager, project)
