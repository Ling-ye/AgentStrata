from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from chatcopilot.botspec.cli import _render_runtime_env, _runtime_env_values
from chatcopilot.botspec.deployment_env import runtime_environment_keys
from chatcopilot.botspec.inspection import declared_configuration, expected_configuration
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.runtime_env import apply_runtime_env
from chatcopilot.core.settings import load_local_env_values
from console.control.gateway_observability import configuration_comparison


def fixture(tmp_path, name="fixture"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "identity.md").write_text("Fixture identity")
    (folder / "rag.yaml").write_text("sources:\n- label: docs\n  path: ${CHATCOPILOT_RAG_ROOT}\n  include: ['*.md']\n  exclude: ['secret*']\n")
    (folder / "repos.yaml").write_text("repositories:\n- id: fixture-repo\n  root: ${CHATCOPILOT_REPO_ROOT}\n  max_read_bytes: 4096\n")
    (folder / "mcp.yaml").write_text("servers:\n- id: fixture-server\n  command: fixture-command\n  args: [serve]\n  enabled: true\n")
    data = {"id": name, "prompts": {"schema_version": 2, "identity": "identity.md"},
            "gateway": {}, "channels": {"qq": {"type": "qq_personal", "provider": "onebot_v11"}},
            "agents": {"backend": "native"}, "tools": {"mcp": {"servers": "mcp.yaml"}},
            "context": {"rag": {"sources": "rag.yaml"}, "codebases": {"registry": "repos.yaml"}}}
    path = folder / "bot.yaml"
    path.write_text(yaml.safe_dump(data))
    values = {"CHATCOPILOT_CHAT_MODEL": name + "-model", "QQ_ALLOW_FROM": "*",
              "CHATCOPILOT_RAG_ROOT": str(folder / "documents"), "CHATCOPILOT_REPO_ROOT": str(folder / "repository"),
              "CHATCOPILOT_WIKI_ROOT": "~/wiki", "GITHUB_MCP_AUTHORIZATION": ""}
    return path, values


def loaded_by_startup(path, values, monkeypatch, tmp_path):
    spec = load_botspec(path)
    exports = tmp_path / (spec.id + ".env")
    exports.write_text(_render_runtime_env(_runtime_env_values(spec, values), runtime_environment_keys(spec)))
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", load_local_env_values(exports))
        runtime = SimpleNamespace(spec=spec, source_path=path, bot_id=spec.id, instance_id=spec.id,
                                  display_name=spec.display_name, workspace_root="", log_dir="")
        apply_runtime_env(runtime)
        return declared_configuration(path, dict(os.environ))


def test_expected_matches_actual_export_and_startup_defaults_without_process_pollution(tmp_path, monkeypatch):
    path, values = fixture(tmp_path)
    monkeypatch.setenv("CHATCOPILOT_CHAT_MODEL", "console-unrelated-model")
    monkeypatch.setenv("QQ_ALLOW_FROM", "console-unrelated-group")
    before = dict(os.environ)
    current = expected_configuration(path, values, home=Path.home())
    assert dict(os.environ) == before
    loaded = loaded_by_startup(path, values, monkeypatch, tmp_path)
    assert configuration_comparison(current, loaded, stale=False)[0] == "applied"
    by_id = {item["id"]: item for item in current["entities"]}
    assert by_id["gateway:instance"]["effective_environment"]["CHATCOPILOT_GATEWAY_PORT"] == "18789"
    assert by_id["context:wiki"]["environment"]["CHATCOPILOT_WIKI_ROOT"] == "~/wiki"
    assert by_id["context:wiki"]["effective_environment"]["CHATCOPILOT_WIKI_ROOT"] == str(Path.home() / "wiki")
    assert by_id["model-slot:chat"]["effective_environment"]["CHATCOPILOT_CHAT_MODEL"] == "fixture-model"
    assert by_id["rag:docs"]["config"]["exclude"] == ["secret*"]
    assert by_id["codebase:fixture-repo"]["config"]["max_read_bytes"] == 4096


@pytest.mark.parametrize("key,value", [("CHATCOPILOT_CHAT_MODEL", "changed-model"), ("QQ_ALLOW_FROM", ""),
                                      ("CHATCOPILOT_GATEWAY_PORT", "18790"), ("CHATCOPILOT_RAG_ROOT", "~/changed"),
                                      ("CHATCOPILOT_REPO_ROOT", "~/changed-repo")])
def test_real_effective_changes_are_pending(tmp_path, monkeypatch, key, value):
    path, values = fixture(tmp_path)
    loaded = loaded_by_startup(path, values, monkeypatch, tmp_path)
    current = expected_configuration(path, {**values, key: value}, home=Path.home())
    assert configuration_comparison(current, loaded, stale=False)[0] == "pending"


def test_empty_value_semantics_and_referenced_files(tmp_path, monkeypatch):
    path, values = fixture(tmp_path)
    loaded = loaded_by_startup(path, values, monkeypatch, tmp_path)
    current = expected_configuration(path, {**values, "GITHUB_MCP_AUTHORIZATION": ""}, home=Path.home())
    assert configuration_comparison(current, loaded, stale=False)[0] == "applied"
    original = copy.deepcopy(loaded)
    for filename in ("repos.yaml", "mcp.yaml", "identity.md"):
        target = path.parent / filename
        old = target.read_text()
        target.write_text(old + "\n# changed reference\n")
        changed = expected_configuration(path, values, home=Path.home())
        assert configuration_comparison(changed, loaded, stale=False)[0] == "pending"
        target.write_text(old)
    assert loaded == original


def test_removed_group_list_does_not_appear_or_create_pending_configuration(tmp_path, monkeypatch):
    path, values = fixture(tmp_path)
    loaded = loaded_by_startup(path, values, monkeypatch, tmp_path)
    current = expected_configuration(path, {**values, "QQ_ALLOW_GROUPS": "invalid-old-value"}, home=Path.home())
    assert configuration_comparison(current, loaded, stale=False)[0] == "applied"
    policy = next(item for item in current["entities"] if item["id"] == "policy:instance")
    assert "QQ_ALLOW_GROUPS" not in policy["config"]


def test_stale_missing_and_incomparable_snapshots_are_unknown(tmp_path, monkeypatch):
    path, values = fixture(tmp_path)
    loaded = loaded_by_startup(path, values, monkeypatch, tmp_path)
    current = expected_configuration(path, values, home=Path.home())
    assert configuration_comparison(current, loaded, stale=True)[0] == "unknown"
    assert configuration_comparison(current, None, stale=False)[0] == "unknown"
    assert configuration_comparison(None, loaded, stale=False)[0] == "unknown"
    loaded.pop("reference_revision")
    assert configuration_comparison(current, loaded, stale=False)[0] == "unknown"
    loaded.pop("environment_revision")
    assert configuration_comparison(current, loaded, stale=False)[0] == "unknown"


def test_two_instance_projections_do_not_share_environment_or_registry_cache(tmp_path, monkeypatch):
    from chatcopilot.external_tools.codebase import config
    first, a = fixture(tmp_path, "first")
    second, b = fixture(tmp_path, "second")
    sentinel = object()
    monkeypatch.setattr(config, "_cached_registry", sentinel)
    before = dict(os.environ)
    one = expected_configuration(first, a, home=Path.home())
    two = expected_configuration(second, b, home=Path.home())
    assert config._cached_registry is sentinel
    assert dict(os.environ) == before
    repositories = [next(item for item in current["entities"] if item["id"] == "codebase:fixture-repo") for current in (one, two)]
    assert repositories[0]["config"]["root"] != repositories[1]["config"]["root"]
    assert one["effective_environment_revision"] != two["effective_environment_revision"]


def test_shared_save_keeps_other_groups_mcp_overrides_and_custom_file(tmp_path):
    from console.control.catalog import bot_tool_config
    from console.control.yaml_editor import apply_tool_config
    path, _ = fixture(tmp_path)
    mcp = path.parent / "mcp.yaml"
    mcp.write_text("servers:\n- ref: git-local\n  enabled: true\n  args: [configured-argument]\n  timeout_seconds: 37\n- id: inline-server\n  command: fixture-command\n")
    config = bot_tool_config(path)
    config["tools"]["packs"] = ["workspace.read_write"]
    config["tools"]["mcp"]["servers"][0]["enabled"] = False
    config["agents"]["presets"] = ["mcp_query"]
    apply_tool_config(path, tool_packs=config["tools"]["packs"], hidden_tools=["hidden-fixture"],
                      mcp_servers=config["tools"]["mcp"]["servers"], agent_presets=config["agents"]["presets"])
    saved = yaml.safe_load(path.read_text())
    assert saved["context"]["rag"]["sources"] == "rag.yaml"
    assert saved["agents"]["backend"] == "native"
    assert saved["agents"]["presets"] == ["mcp_query"]
    assert saved["tools"]["packs"] == ["workspace.read_write"]
    servers = yaml.safe_load(mcp.read_text())["servers"]
    git = next(item for item in servers if item.get("ref") == "git-local")
    assert git == {"ref": "git-local", "enabled": False, "args": ["configured-argument"], "timeout_seconds": 37}
    assert any(item.get("id") == "inline-server" for item in servers)
    assert not (path.parent / "mcp" / "servers.yaml").exists()


def test_registry_uses_derived_runtime_root_even_when_not_saved_in_env(tmp_path):
    path, values = fixture(tmp_path)
    (path.parent / "repos.yaml").write_text("repositories:\n- id: fixture-repo\n  root: ${CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT}\n")
    current = expected_configuration(path, values, home=Path.home())
    assert any(item["id"] == "codebase:fixture-repo" for item in current["entities"])
    assert not any(item["id"] == "context:codebase-details" for item in current["entities"])


def test_template_environment_changes_and_comparison_version(tmp_path, monkeypatch):
    path, values = fixture(tmp_path)
    fallback = str(tmp_path / "fallback")
    (path.parent / "repos.yaml").write_text("repositories:\n- id: fixture-repo\n  root: ${CHATCOPILOT_REPO_ROOT:-" + fallback + "}\n")
    loaded = loaded_by_startup(path, values, monkeypatch, tmp_path)
    current = expected_configuration(path, {**values, "CHATCOPILOT_REPO_ROOT": "~/new-repository"}, home=Path.home())
    assert configuration_comparison(current, loaded, stale=False)[0] == "pending"
    loaded["environment_revision_version"] = -1
    assert configuration_comparison(current, loaded, stale=False)[0] == "unknown"
