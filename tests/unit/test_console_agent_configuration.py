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
            "llm": {"chat": {"env_prefix": "CHATCOPILOT_FIXTURE"}, "research": {"env_prefix": "CHATCOPILOT_FIXTURE_RESEARCH", "model": "research-default"},
                    "code": {"enabled": True, "model": "codex-default", "reasoning_effort": "medium",
                             "profiles": {"worker": {"model": "worker-default", "reasoning_effort": "high"}}, "code_task_profile": "worker"}},
            "agents": {"runtime": runtime_id, **agents}}
    path = tmp_path / "bot.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def projected(path, values):
    data = expected_configuration(path, values, home=path.parent)
    return {item["id"]: item for item in data["entities"]}


@pytest.mark.parametrize("backend,model", [("codex", "chat-fixture"), ("native", "chat-fixture"), ("langgraph", "chat-fixture")])
def test_instance_models_match_backend_and_full_environment_overrides(tmp_path, monkeypatch, backend, model):
    path = bot(tmp_path, backend)
    monkeypatch.setenv("CHATCOPILOT_FIXTURE_MODEL", "unrelated-console-model")
    before = dict(os.environ)
    values = {"CHATCOPILOT_FIXTURE_MODEL": "chat-fixture", "CHATCOPILOT_FIXTURE_BASE_URL": "https://example.invalid/v1", "CHATCOPILOT_FIXTURE_API_KEY": "fixture-key",
              "CHATCOPILOT_FIXTURE_TIMEOUT": "41", "CHATCOPILOT_FIXTURE_RESEARCH_MODEL": "research-override", "CHATCOPILOT_FIXTURE_RESEARCH_TIMEOUT": "17",
              "CHATCOPILOT_FIXTURE_CODE_MODEL": "codex-override", "CHATCOPILOT_FIXTURE_CODE_REASONING_EFFORT": "high",
              "CHATCOPILOT_FIXTURE_CODE_PROFILES_JSON": json.dumps({"changed": {"model": "worker-override", "reasoning_effort": "medium"}}),
              "CHATCOPILOT_FIXTURE_CODE_TASK_PROFILE": "changed"}
    rows = projected(path, values)
    assert rows["agent:main"]["effective_config"]["model"] == model
    research = rows["model-slot:research"]["effective_config"]
    assert {key: research[key] for key in ("base_url", "model", "timeout")} == {
        "base_url": "https://example.invalid/v1", "model": "research-override", "timeout": 17}
    assert "api_key" not in research and "fixture-key" not in json.dumps(research)
    assert rows["model-slot:research"]["field_sources"]["base_url"] == "继承基础模型 · chat"
    assert "CHATCOPILOT_FIXTURE_RESEARCH_MODEL" in rows["model-slot:research"]["field_sources"]["model"]
    code = rows["model-slot:code"]["effective_config"]
    assert set(code["profiles"]) == {"changed"}
    assert code["code_task_profile"] == "changed"
    assert rows["agent:code-task"]["effective_config"]["model"] == "worker-override"
    assert rows["agent:code-task"]["configured"] is False
    assert dict(os.environ) == before
    assert projected(path, {"CHATCOPILOT_FIXTURE_MODEL": "second-model"})["model-slot:chat"]["effective_config"]["model"] == "second-model"


def test_inspection_definitions_and_budgets_match_runtime_resolver(tmp_path, monkeypatch):
    path = bot(tmp_path, presets=["mcp_query"], defaults={"max_tool_calls": 11, "timeout_seconds": 150},
        mcp_query={"timeout_seconds": 42, "context_policy": {"max_context_tokens": 2400}},
        custom=[{"name": "custom_reader", "tool_name": "read_custom", "summary": "Read fixture", "prompt": {"role": "identity.md"},
                 "selector": {"any": [{"names": ["read_file"]}]}, "budget": {"model_env_prefix": "CHATCOPILOT_ALT", "max_tool_calls": 2}}])
    def forbid(*args, **kwargs):
        pytest.fail("inspection must not construct an LLM client")
    monkeypatch.setattr("chatcopilot.core.llm_client.LLMClient.__init__", forbid)
    rows = projected(path, {"CHATCOPILOT_FIXTURE_MODEL": "chat-fixture", "CHATCOPILOT_ALT_MODEL": "custom-model"})
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
    values = {"CHATCOPILOT_FIXTURE_MODEL": "fixture-model"}
    original = configuration_projection(spec, environment=values)
    enriched = copy.deepcopy(original)
    enrich_agent_configuration(enriched, spec, values)
    assert enriched["environment_revision"] == original["environment_revision"]
    assert enriched["environment_revision_version"] == original["environment_revision_version"]
    assert "effective_config" not in original["entities"][0]


def test_core_loader_has_explicit_environment_and_config_search_context(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  model: file-model\nruntime:\n  max_context_tokens: 1234\n")
    monkeypatch.setenv("CHATCOPILOT_FIXTURE_MODEL", "console-model")
    sources = {}
    loaded = load_config(env_prefix="CHATCOPILOT_FIXTURE", environment={}, default_paths=(config,), sources=sources)
    assert loaded.llm.model == "file-model"
    assert loaded.runtime.max_context_tokens == 1234
    assert "配置文件" in sources["llm.model"]
    loaded = load_config(env_prefix="CHATCOPILOT_FIXTURE", environment={"CHATCOPILOT_FIXTURE_MODEL": "instance-model"}, default_paths=())
    assert loaded.llm.model == "instance-model"
    assert loaded.runtime.max_context_tokens == 16000
    loaded = load_config(env_prefix="CHATCOPILOT_FIXTURE", environment={"CHATCOPILOT_FIXTURE_CONFIG": "config.yaml"},
                         default_paths=(), working_directory=tmp_path)
    assert loaded.llm.model == "file-model"


def test_unexported_model_override_is_explained_without_changing_deployment_rules(tmp_path):
    path = bot(tmp_path, presets=["mcp_query"], mcp_query={"model_env_prefix": "NONEXPORTED"})
    rows = projected(path, {"CHATCOPILOT_FIXTURE_MODEL": "base-model", "NONEXPORTED_MODEL": "saved-only"})
    custom = rows["subagent:mcp_query"]
    assert custom["effective_config"]["model"] == "saved-only"
    assert "NONEXPORTED_MODEL" in custom["field_sources"]["model"]


def test_search_provider_ids_do_not_collide_with_search_controls(tmp_path):
    path = bot(tmp_path, unified_search={"enabled": True, "providers": [{"id": "unified", "kind": "brave"}]})
    rows = projected(path, {})
    assert rows["search:unified"]["effective_config"]["kind"] == "brave"
    assert "budget" in rows["agent:unified-search"]["effective_config"]
