from __future__ import annotations

import copy
import json
import os

import pytest
import yaml

from chatcopilot.botspec.inspection import configuration_projection, expected_configuration
from chatcopilot.botspec.inspection_agent import enrich_agent_configuration
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.component_catalog.subagent_resolution import iter_definitions
from chatcopilot.core.config import load_config


def bot(tmp_path, runtime_id="codex", **agents):
    (tmp_path / "identity.md").write_text("Fixture identity")
    data = {"id": "fixture", "display_name": "Fixture", "prompts": {"schema_version": 2, "identity": "identity.md", "response_style": "identity.md"},
            "gateway": {}, "channels": {"qq": {"type": "qq_personal", "provider": "onebot_v11"}},
            "llm": {"chat": {"env_prefix": "CHATCOPILOT_FIXTURE", "binding": "chat"},
                    "research": {"binding": "research"}, "code": {"enabled": True, "binding": "code"}},
            "agents": {"runtime": runtime_id, **agents}}
    path = tmp_path / "bot.yaml"
    path.write_text(yaml.safe_dump(data))
    from tests.model_settings_fixture import test_model_document, write_models
    write_models(tmp_path / "models.json", test_model_document())
    return path


def projected(path, values):
    data = expected_configuration(path, {"AGENTSTRATA_LLM_CONFIG": str(path.parent / "models.json"), **values}, home=path.parent)
    return {item["id"]: item for item in data["entities"]}


@pytest.mark.parametrize("backend,model", [("codex", "chat-fixture"), ("native", "chat-fixture"), ("langgraph", "chat-fixture")])
def test_instance_models_match_backend_and_full_environment_overrides(tmp_path, monkeypatch, backend, model):
    path = bot(tmp_path, backend)
    settings = path.parent / "models.json"
    data = json.loads(settings.read_text())
    data["connections"]["api"].update(kind="openai_responses", base_url="https://api.openai.com/v1", timeout=41)
    data["connections"]["research"] = {"kind": "openai_compatible", "base_url": "https://example.invalid/v1", "timeout": 17,
        "auth": {"mode": "api_key", "key_env": "FIXTURE_KEY"}}
    data["profiles"]["chat"]["model"] = "chat-fixture"
    data["profiles"]["research"] = {"connection": "research", "model": "research-override"}
    data["profiles"]["changed"] = {"connection": "worker", "model": "worker-override", "reasoning_effort": "medium"}
    data["bindings"]["code"] = "changed"
    settings.write_text(json.dumps(data))
    before = dict(os.environ)
    rows = projected(path, {"FIXTURE_KEY": "fixture-key"})
    assert rows["agent:main"]["effective_config"]["model"] == model
    research = rows["model-slot:research"]["effective_config"]
    assert {key: research[key] for key in ("base_url", "model", "timeout")} == {
        "base_url": "https://example.invalid/v1", "model": "research-override", "timeout": 17}
    assert "api_key" not in research and "fixture-key" not in json.dumps(research)
    assert rows["model-slot:research"]["field_sources"]["model"] == "统一模型配置"
    assert rows["model-slot:code"]["effective_config"]["code_task_profile"] == "changed"
    assert rows["agent:code-task"]["effective_config"]["model"] == "worker-override"
    assert rows["agent:code-task"]["configured"] is False
    assert dict(os.environ) == before
    data["profiles"]["chat"]["model"] = "second-model"
    settings.write_text(json.dumps(data))
    assert projected(path, {})["model-slot:chat"]["effective_config"]["model"] == "second-model"


def test_inspection_definitions_and_budgets_match_runtime_resolver(tmp_path, monkeypatch):
    path = bot(tmp_path, presets=["mcp_query"], defaults={"max_tool_calls": 11, "timeout_seconds": 150},
        mcp_query={"timeout_seconds": 42, "context_policy": {"max_context_tokens": 2400}},
        custom=[{"name": "custom_reader", "tool_name": "read_custom", "summary": "Read fixture", "prompt": {"role": "identity.md"},
                 "selector": {"any": [{"names": ["read_file"]}]}, "budget": {"model_binding": "CHATCOPILOT_ALT", "max_tool_calls": 2}}])
    def forbid(*args, **kwargs):
        pytest.fail("inspection must not construct an LLM client")
    monkeypatch.setattr("chatcopilot.core.llm_client.LLMClient.__init__", forbid)
    settings = path.parent / "models.json"
    data = json.loads(settings.read_text())
    data["profiles"]["custom"] = {"connection": "api", "model": "custom-model"}
    data["bindings"]["CHATCOPILOT_ALT"] = "custom"
    settings.write_text(json.dumps(data))
    rows = projected(path, {})
    spec = load_botspec(path)
    for definition, budget in iter_definitions(spec.agents):
        row = rows["subagent:" + definition.name]
        assert row["effective_config"]["budget"]["timeout_seconds"] == budget.timeout_seconds
        assert row["effective_config"]["context_policy"]["max_context_tokens"] == definition.context_policy.max_context_tokens
    assert rows["subagent:mcp_query"]["effective_config"]["budget"]["max_tool_calls"] == 11
    assert rows["subagent:custom_reader"]["membership"] == "custom"
    assert rows["subagent:custom_reader"]["effective_config"]["model"] == "custom-model"
    assert rows["subagent:custom_reader"]["effective_config"]["budget"]["max_tool_calls"] == 2
    assert rows["subagent:custom_reader"]["field_sources"]["budget.timeout_seconds"] == "BotSpec · agents.defaults.timeout_seconds"
    assert rows["subagent:custom_reader"]["field_sources"]["budget.max_model_turns"] == "代码默认值"
    assert rows["subagent:mcp_query"]["field_sources"]["budget.timeout_seconds"] == "BotSpec · agents.mcp_query.timeout_seconds"


def test_defaults_disabled_providers_and_fixed_policy_remain_visible(tmp_path):
    path = bot(tmp_path, unified_search={"enabled": False, "providers": [{"id": "brave", "kind": "brave", "enabled": False}]})
    rows = projected(path, {})
    assert rows["search:brave"]["configured"] is False
    assert rows["search:brave"]["effective_config"]["endpoint"] == "https://api.search.brave.com/res/v1/web/search"
    assert rows["search:brave"]["effective_config"]["credential_env"] == "BRAVE_API_KEY"
    assert rows["search:brave"]["field_sources"]["max_results"] == "代码默认值"
    assert rows["agent:unified-search"]["configured"] is False
    assert rows["agent:runtime"]["effective_config"]["hard_iteration_cap"] is None
    assert rows["agent:host-policy"]["field_sources"]["network_access"] == "宿主固定策略"
    assert "codex" not in rows["agent:main"]["effective_config"]
    assert rows["model-slot:research"]["effective_config"]["model"] == "research-default"


def test_presentation_metadata_does_not_change_declaration_fingerprints(tmp_path):
    path = bot(tmp_path)
    spec = load_botspec(path)
    values = {"AGENTSTRATA_LLM_CONFIG": str(path.parent / "models.json")}
    original = configuration_projection(spec, environment=values)
    enriched = copy.deepcopy(original)
    enrich_agent_configuration(enriched, spec, values)
    assert enriched["environment_revision"] == original["environment_revision"]
    assert enriched["environment_revision_version"] == original["environment_revision_version"]
    assert "effective_config" not in original["entities"][0]


def test_core_loader_has_explicit_environment_and_config_search_context(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("runtime:\n  max_context_tokens: 1234\n")
    monkeypatch.setenv("CHATCOPILOT_FIXTURE_MODEL", "console-model")
    sources = {}
    loaded = load_config(env_prefix="CHATCOPILOT_FIXTURE", environment={}, default_paths=(config,), sources=sources)
    assert loaded.llm.model == "gpt-4o-mini"
    assert loaded.runtime.max_context_tokens == 1234
    with pytest.raises(ValueError, match="旧模型"):
        load_config(env_prefix="CHATCOPILOT_FIXTURE", environment={"CHATCOPILOT_FIXTURE_MODEL": "instance-model"})
    config.write_text("llm:\n  model: retired\n")
    with pytest.raises(ValueError, match="旧模型"):
        load_config(config, environment={})


def test_unexported_model_override_is_explained_without_changing_deployment_rules(tmp_path):
    path = bot(tmp_path, presets=["mcp_query"], mcp_query={"model_binding": "custom"})
    settings = path.parent / "models.json"
    data = json.loads(settings.read_text())
    data["profiles"]["custom"] = {"connection": "api", "model": "saved-only"}
    data["bindings"]["custom"] = "custom"
    settings.write_text(json.dumps(data))
    custom = projected(path, {})["subagent:mcp_query"]
    assert custom["effective_config"]["model"] == "saved-only"
    assert custom["field_sources"]["model"] == "统一模型配置"


def test_search_provider_ids_do_not_collide_with_search_controls(tmp_path):
    path = bot(tmp_path, unified_search={"enabled": True, "providers": [{"id": "unified", "kind": "brave"}]})
    rows = projected(path, {})
    assert rows["search:unified"]["effective_config"]["kind"] == "brave"
    assert "budget" in rows["agent:unified-search"]["effective_config"]
