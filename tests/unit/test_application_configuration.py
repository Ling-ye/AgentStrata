"""Application/profile boundaries, isolation and explicit data-preserving migration."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.profile_migration import migrate_agent_profile
from chatcopilot.botspec.runtime import assemble_runtime_context
from chatcopilot.core.application_configuration import load_application_spec


def application(root: Path, name: str, *, runtime: str = "langgraph", nested: bool = False) -> Path:
    directory = root / name
    directory.mkdir()
    profile_dir = directory / "profile" if nested else directory
    profile_dir.mkdir(exist_ok=True)
    (profile_dir / "prompts").mkdir()
    (profile_dir / "prompts/identity.md").write_text(name)
    (profile_dir / "prompts/style.md").write_text("concise")
    profile = {
        "llm": {"chat": {"binding": name + ".chat"}, "research": {"binding": name + ".chat"}},
        "prompts": {"schema_version": 2, "identity": "prompts/identity.md", "response_style": "prompts/style.md"},
        "agents": {"runtime": runtime},
        "tools": {"packs": ["workspace.read_write", "memory.chat"]},
        "context": {"dev": {"root_env": name.upper().replace("-", "_") + "_PROJECT"}},
    }
    (profile_dir / "agent.yaml").write_text(yaml.safe_dump(profile))
    data = {"id": name, "display_name": name,
            "agent_profile": "profile/agent.yaml" if nested else "agent.yaml",
            "gateway": {"clients": [{"id": "local-owner", "token_env": "LOCAL_TOKEN", "role": "owner"}]},
            "deploy": {"instance_id": name, "workspace_root": str(directory / "workspace")}}
    path = directory / "bot.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def legacy_application(root: Path) -> Path:
    path = application(root, "legacy-app")
    data = load_application_spec(path).resolved_configuration()
    del data["agent_profile"]
    (path.parent / "agent.yaml").unlink()
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return path


def test_application_profile_uses_existing_typed_assembly(tmp_path):
    path = application(tmp_path, "local-app", nested=True)
    spec = load_botspec(path)
    assert not [issue for issue in validate_botspec(spec) if issue.level == "error"]
    assert spec.source_path == path
    assert spec.application.agent_profile.source_path == path.parent / "profile/agent.yaml"
    assert spec.resolve_path(spec.prompts.identity) == path.parent / "profile/prompts/identity.md"
    assert spec.resolve_application_path("package.yaml") == path.parent / "package.yaml"
    assert assemble_runtime_context(spec).prompt_profile.identity == "local-app"


def test_resolved_configuration_cannot_mutate_profile_or_another_application(tmp_path):
    first = load_application_spec(application(tmp_path, "first-app"))
    second = load_application_spec(application(tmp_path, "second-app"))
    projected = first.resolved_configuration()
    projected["tools"]["packs"].clear()
    assert first.agent_profile.raw["tools"]["packs"]
    assert second.agent_profile.raw["tools"]["packs"]


def test_runtime_model_prompt_tools_and_project_roots_are_independent(tmp_path):
    from chatcopilot.application.agent_runtime import project_agent_runtime
    from chatcopilot.core.config import ChatConfig
    from chatcopilot.core.model_settings import worker_profile

    first = application(tmp_path, "first-app", runtime="codex")
    second = application(tmp_path, "second-app", runtime="langgraph")
    models = tmp_path / "models.json"
    document = {
        "connections": {
            "api": {"kind": "openai_responses", "auth": {"mode": "api_key", "key_env": "TEST_API_KEY"}},
            "worker": {"kind": "codex", "auth": {"mode": "chatgpt", "profile": "worker"}},
        },
        "profiles": {
            "first": {"connection": "api", "model": "first-model"},
            "second": {"connection": "api", "model": "second-model"},
            "harness": {"connection": "worker", "model": "harness-model"},
            "harness-alt": {"connection": "worker", "model": "harness-alternative"},
        },
        "bindings": {"first-app.chat": "first", "second-app.chat": "second", "harness": "harness"},
    }
    models.write_text(json.dumps(document))
    environment = {"AGENTSTRATA_LLM_CONFIG": str(models),
                   "FIRST_APP_PROJECT": str(first.parent / "project"),
                   "SECOND_APP_PROJECT": str(second.parent / "project")}
    original_environment = dict(environment)
    runtime_a = assemble_runtime_context(load_botspec(first))
    runtime_b = assemble_runtime_context(load_botspec(second))
    config = ChatConfig()
    a = project_agent_runtime(runtime_a, chat_config=config, environment=environment)
    b = project_agent_runtime(runtime_b, chat_config=config, environment=environment)
    assert (a.route.runtime_id, b.route.runtime_id) == ("codex", "langgraph")
    assert (a.chat_config.llm.model, b.chat_config.llm.model) == ("first-model", "second-model")
    assert a.project_roots == (first.parent / "project",)
    assert b.project_roots == (second.parent / "project",)
    assert runtime_a.workspace_root != runtime_b.workspace_root
    profile_path = first.parent / "agent.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["llm"]["chat"]["binding"] = "second-app.chat"
    profile["tools"]["packs"] = ["memory.chat"]
    profile_path.write_text(yaml.safe_dump(profile))
    (first.parent / "prompts/identity.md").write_text("changed first persona")
    changed = assemble_runtime_context(load_botspec(first))
    changed_projection = project_agent_runtime(changed, chat_config=config, environment=environment)
    assert changed_projection.chat_config.llm.model == "second-model"
    assert changed.tool_packs == ("memory.chat",)
    assert runtime_a.prompt_profile.identity == "first-app"
    assert runtime_b.prompt_profile.identity == "second-app"
    assert "workspace.read_write" in runtime_b.tool_packs
    assert a.chat_config.llm.model == "first-model"
    harness, _ = worker_profile("harness-alt", environment=environment)
    assert harness.model == "harness-alternative"
    assert json.loads(models.read_text()) == document
    assert environment == original_environment


@pytest.mark.parametrize("reference", ["../outside.yaml", "/tmp/outside.yaml", "bot.yaml"])
def test_profile_reference_cannot_escape_or_recurse(tmp_path, reference):
    path = application(tmp_path, "local-app")
    data = yaml.safe_load(path.read_text())
    data["agent_profile"] = reference
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="agent_profile"):
        load_botspec(path)


def test_profile_cannot_own_application_identity_or_shadow_inline_settings(tmp_path):
    path = application(tmp_path, "local-app")
    profile_path = path.parent / "agent.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    invalid = deepcopy(profile)
    invalid["gateway"] = {"clients": []}
    profile_path.write_text(yaml.safe_dump(invalid))
    with pytest.raises(ValueError, match="AgentProfile"):
        load_botspec(path)
    profile_path.write_text(yaml.safe_dump(profile))
    data = yaml.safe_load(path.read_text())
    data["agents"] = {"runtime": "codex"}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="不能.*内联"):
        load_botspec(path)


def test_migration_is_explicit_backed_up_and_preserves_data(tmp_path):
    path = legacy_application(tmp_path)
    original = path.read_bytes()
    persisted = path.parent / "workspace"
    persisted.mkdir()
    (persisted / "memory.txt").write_text("existing private memory")
    with pytest.raises(ValueError, match="migrate-profile"):
        load_botspec(path)
    preview = migrate_agent_profile(path)
    assert preview["changed"] and not preview["applied"]
    assert path.read_bytes() == original
    assert not (path.parent / "agent.yaml").exists()
    result = migrate_agent_profile(path, apply=True)
    assert result["applied"]
    assert Path(result["backup"]).read_bytes() == original
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    spec = load_botspec(path)
    assert {k: v for k, v in spec.raw.items() if k != "agent_profile"} == yaml.safe_load(original)
    assert (persisted / "memory.txt").read_text() == "existing private memory"
    assert migrate_agent_profile(path, apply=True)["changed"] is False


def test_migration_never_overwrites_an_existing_profile(tmp_path):
    path = legacy_application(tmp_path)
    before = path.read_bytes()
    target = path.parent / "agent.yaml"
    target.write_text("existing user data\n")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migrate_agent_profile(path, apply=True)
    assert path.read_bytes() == before
    assert target.read_text() == "existing user data\n"


def test_interrupted_migration_can_finish_without_replacing_generated_profile(tmp_path, monkeypatch):
    from chatcopilot.botspec import profile_migration
    path = legacy_application(tmp_path)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("interrupted publication")
        patch.setattr(profile_migration.os, "replace", fail)
        with pytest.raises(OSError, match="interrupted"):
            migrate_agent_profile(path, apply=True)
    assert path.read_bytes() == before
    profile = path.parent / "agent.yaml"
    inode = profile.stat().st_ino
    assert migrate_agent_profile(path, apply=True)["applied"]
    assert profile.stat().st_ino == inode
    assert load_botspec(path).id == "legacy-app"


def test_console_edits_profile_and_preserves_application(tmp_path):
    from console.control.catalog import bot_tool_config
    from console.control.yaml_editor import apply_tool_config
    path = application(tmp_path, "local-app", nested=True)
    before = path.read_bytes()
    result = apply_tool_config(path, tool_packs=["memory.chat"], agent_presets=[])
    assert result["files_modified"] == [str(path.parent / "profile/agent.yaml")]
    assert path.read_bytes() == before
    assert bot_tool_config(path)["tools"]["packs"] == ["memory.chat"]


def test_migration_does_not_reuse_a_backup_with_exposed_permissions(tmp_path, monkeypatch):
    from chatcopilot.botspec import profile_migration
    path = legacy_application(tmp_path)
    before = path.read_bytes()
    backup = Path(migrate_agent_profile(path)["backup"])
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("interrupted publication")
        patch.setattr(profile_migration.os, "replace", fail)
        with pytest.raises(OSError):
            migrate_agent_profile(path, apply=True)
    backup.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        migrate_agent_profile(path, apply=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("runtime", "codex"), ("binding", "other.chat"), ("root_env", "OTHER_PROJECT")])
def test_profile_candidate_cannot_change_execution_authority(tmp_path, field, value):
    from chatcopilot.core.candidate_configuration import configuration_path, validate_configuration
    path = application(tmp_path, "local-app")
    before = (path.parent / "agent.yaml").read_bytes()
    data = yaml.safe_load(before)
    if field == "runtime":
        data["agents"][field] = value
    elif field == "binding":
        data["llm"]["chat"][field] = value
    else:
        data["context"]["dev"][field] = value
    assert configuration_path("bots/local-app/agent.yaml", "local-app")
    assert not configuration_path("bots/other-app/agent.yaml", "local-app")
    with pytest.raises(ValueError, match="authority"):
        validate_configuration(before, yaml.safe_dump(data).encode())
    data = yaml.safe_load(before)
    data["tools"]["packs"] = ["memory.chat"]
    validate_configuration(before, yaml.safe_dump(data).encode())


def test_application_candidate_cannot_change_profile_reference(tmp_path):
    from chatcopilot.core.candidate_configuration import validate_configuration
    path = application(tmp_path, "local-app")
    before = path.read_bytes()
    data = yaml.safe_load(before)
    data["agent_profile"] = "other.yaml"
    with pytest.raises(ValueError, match="authority"):
        validate_configuration(before, yaml.safe_dump(data).encode())
