"""Read-only Agent fields, using the same model and definition resolvers as execution.

Presentation fields are added after the declaration/environment fingerprints.
This module never constructs a client, provider, registry or session.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from chatcopilot.component_catalog.subagent_resolution import iter_definitions, iter_workflows
from chatcopilot.core.config import ChatConfig, LLMConfig, load_config, load_llm_profile
from chatcopilot.core.inspection import plain
from chatcopilot.contracts.subagents import DEFAULT_PROVIDER_ENDPOINTS, DEFAULT_PROVIDER_CREDENTIAL_ENVS
from .model import BotSpec
from .runtime_env import _source_root, load_research_llm_config


def resolve_inspection_config(spec: BotSpec, environment: Mapping[str, str]) -> tuple[ChatConfig, dict[str, str]]:
    sources: dict[str, str] = {}
    root = _source_root(spec.source_path)
    config_dir = Path(environment.get("CHATCOPILOT_CONFIG_DIR") or Path.home() / ".chatcopilot").expanduser()
    config = load_config(env_prefix=spec.llm.env_prefix, environment=environment,
                         default_paths=(root / "src/chatcopilot/agent/config.yaml", config_dir / "chat.yaml"),
                         working_directory=Path(environment.get("CHATCOPILOT_HOME") or root),
                         sources=sources)
    return config, sources


def enrich_agent_configuration(
    projection: dict[str, Any], spec: BotSpec, environment: Mapping[str, str],
    *, chat_config: ChatConfig | None = None, sources: dict[str, str] | None = None,
    saved_environment: Mapping[str, str] | None = None,
    backend: str | None = None, research_config: LLMConfig | None = None, search_config: LLMConfig | None = None,
) -> None:
    if chat_config is None:
        chat_config, sources = resolve_inspection_config(spec, environment)
    sources = sources or {}
    saved = environment if saved_environment is None else saved_environment
    entities = {item["id"]: item for item in projection["entities"]}
    prefix = spec.llm.env_prefix
    backend = spec.agents.backend if backend is None else backend
    research = research_config or load_research_llm_config(spec.llm, fallback=chat_config.llm, environment=environment)
    raw_agents = spec.raw.get("agents") or {}

    def add(identity: str, name: str, config: Any, *, usage: str = "", field_sources=None,
            configured: bool = True, **metadata: Any) -> dict[str, Any]:
        entity = entities.get(identity)
        if entity is None:
            entity = {"id": identity, "layer": "agent", "name": name, "config": {},
                      "configured": configured, "loaded": None, "connected": None, "available": None}
            entities[identity] = entity
            projection["entities"].append(entity)
        entity.update(name=name, effective_config=plain(config), usage=usage,
                      field_sources=field_sources or {}, **metadata)
        return entity

    def spec_source(path: str) -> str:
        raw: Any = spec.raw
        for key in path.split("."):
            if not isinstance(raw, dict) or key not in raw:
                return "代码默认值"
            raw = raw[key]
        return "BotSpec · " + path

    def model_sources(model_prefix: str | None, fallback: str, *, research_slot: bool = False) -> dict[str, str]:
        result = {}
        for field in ("model", "base_url", "api_key", "timeout"):
            key = f"{model_prefix}_{field.upper()}" if model_prefix else ""
            if key and environment.get(key):
                result[field] = "环境覆盖 " + key
            elif research_slot and field == "model" and spec.llm.research_model:
                result[field] = "BotSpec · llm.research.model"
            else:
                result[field] = fallback
            if key and saved.get(key) and not environment.get(key):
                result[field] += f"；保存的 {key} 未进入运行环境"
        return result

    def budget_sources(raw: dict[str, Any], path: str, budget: Any, *, research_default: bool = False) -> dict[str, str]:
        result = {}
        for key in plain(budget):
            if key in raw:
                result[key] = f"BotSpec · {path}.{key}"
            elif key == "model_env_prefix" and research_default and spec.agents.defaults.model_env_prefix is None and spec.llm.research_env_prefix:
                result[key] = "继承 llm.research.env_prefix"
            else:
                result[key] = spec_source("agents.defaults." + key)
        return result

    chat = add("model-slot:chat", "基础模型 · chat", plain(chat_config.llm),
        usage="Native / LangGraph 主模型；也是辅助模型的继承基础。Codex 主会话使用 code 槽。",
        field_sources={key: sources.get("llm." + key, "运行装配配置") for key in vars(chat_config.llm)})
    chat["effective_environment"] = {f"{prefix}_{key.upper()}": environment.get(f"{prefix}_{key.upper()}")
                                       for key in vars(chat_config.llm)}
    add("model-slot:research", "研究模型 · research", plain(research),
        usage="供统一搜索、人格研究等能力使用；未覆盖的连接参数逐字段继承基础模型。",
        field_sources=model_sources(spec.llm.research_env_prefix, "继承基础模型 · chat", research_slot=True))
    routing = plain(chat_config.routing)
    code = {key.removeprefix("code_"): value for key, value in routing.items()}
    code["code_task_profile"] = code.pop("task_profile")
    code["enabled"] = spec.llm.code.enabled
    code_sources = {}
    for field in code:
        suffix = "CODE_PROFILES_JSON" if field == "profiles" else "CODE_TASK_PROFILE" if field == "code_task_profile" else "CODE_" + field.upper()
        key = f"{prefix}_{suffix}"
        code_sources[field] = ("环境覆盖 " + key if field != "enabled" and saved.get(key) else spec_source("llm.code." + field))
    add("model-slot:code", "Codex 模型与配置档 · code", code,
        usage="Codex 实例默认模型与可选配置档。模型切换命令只改变当前会话；代码任务按独立配置档执行。",
        field_sources=code_sources,
        applicability="当前 Backend 为 Codex" if backend == "codex" else "不适用于当前主 Agent；保留 Codex 配置")
    slot = "code" if backend == "codex" else "chat"
    add("agent:main", "主 Agent", {"backend": backend, "model": code["model"] if slot == "code" else chat_config.llm.model,
        "reasoning_effort": code["reasoning_effort"] if slot == "code" else None, "model_slot": slot},
        usage="实例默认模型；会话切换后的实际模型请查看对应任务记录。",
        field_sources={"backend": spec_source("agents.backend"), "model": f"引用模型槽 · {slot}",
                       "reasoning_effort": "引用模型槽 · code" if slot == "code" else "当前 Backend 不适用"},
        refs=[f"model-slot:{slot}"])
    budget = plain(chat_config.runtime)
    # Topic routing belongs to Application, not to the Agent loop.
    add("agent:runtime", "循环与上下文参数", {key: value for key, value in budget.items() if not key.startswith("topic_")},
        usage="Native / LangGraph 主循环及辅助 Agent 的上下文与预算。软限制触发健康检查，硬限制终止执行；Codex 整轮超时见 code 槽。",
        field_sources={key: sources.get("runtime." + key, "运行装配配置") for key in budget if not key.startswith("topic_")},
        applicability="不控制 Codex 主会话的循环" if slot == "code" else "适用于当前主 Agent")
    policy = plain(spec.agents.codex)
    web_search = policy.pop("web_search_mode")
    add("agent:host-policy", "Codex 宿主固定策略", policy,
        usage="由宿主代码决定；BotSpec 不接受 agents.codex 配置块。资源权限仍由宿主绑定。",
        field_sources={key: "宿主固定策略" for key in policy},
        applicability="适用于 Codex" if slot == "code" else "当前 Backend 不适用")
    add("agent:codex-web-search", "Codex 原生 Web 搜索", {"web_search_mode": web_search},
        usage="Codex 原生搜索策略，与 search_information 统一搜索是两个入口。",
        field_sources={"web_search_mode": "宿主固定策略"},
        applicability="适用于 Codex" if slot == "code" else "当前 Backend 不适用")
    search_prefix = spec.agents.research_budget.model_env_prefix
    search_model = search_config or (load_llm_profile(search_prefix, fallback=research, environment=environment) if search_prefix else research)
    add("agent:unified-search", "统一搜索", {"enabled": spec.agents.research_enabled, "model": search_model.model,
        "budget": plain(spec.agents.research_budget)},
        usage="search_information 的模型与执行预算；provider 的启用状态独立列出。",
        configured=spec.agents.research_enabled,
        field_sources={"enabled": spec_source("agents.unified_search.enabled"), "model": model_sources(search_prefix, "继承研究模型 · research")["model"],
                       **{"budget." + key: value for key, value in budget_sources(raw_agents.get("unified_search") or {},
                           "agents.unified_search", spec.agents.research_budget, research_default=True).items()}}, refs=["model-slot:research"])
    add("agent:search-budget", "MCP 搜索子任务预算", plain(spec.agents.search_budget),
        usage="用于 risk=search 的 MCP 搜索子 Agent；没有此类来源时不参与执行。",
        field_sources=budget_sources(raw_agents.get("search_budget") or {}, "agents.search_budget", spec.agents.search_budget, research_default=True))
    add("agent:delegation", "委托公共配置", {"defaults": plain(spec.agents.defaults), "max_workflow_depth": spec.agents.max_workflow_depth,
        "presets": list(spec.agents.include), "custom_agents": [item.name for item in spec.agents.custom], "workflows": list(spec.agents.workflows)},
        usage="公共预算是子任务的默认值；每个子 Agent 的最终预算在其条目中展示。",
        field_sources={**{"defaults." + key: spec_source("agents.defaults." + key) for key in plain(spec.agents.defaults)},
                       "max_workflow_depth": spec_source("agents.max_workflow_depth")})
    profile = chat_config.routing.code_profiles.get(chat_config.routing.code_task_profile)
    add("agent:code-task", "独立代码任务", {"enabled": "dev.code_tasks" in spec.tools.packs,
        "code_task_profile": chat_config.routing.code_task_profile or None, "model": profile.model if profile else None,
        "reasoning_effort": profile.reasoning_effort if profile else None},
        configured="dev.code_tasks" in spec.tools.packs,
        usage="独立 code-worker 使用所选配置档，不随主会话的 /model 切换。",
        field_sources={"enabled": "BotSpec · tools.packs", "code_task_profile": code_sources["code_task_profile"],
                       "model": "引用 code 槽配置档", "reasoning_effort": "引用 code 槽配置档"}, refs=["model-slot:code", "pack:dev.code_tasks"])
    custom_names = {item.name for item in spec.agents.custom}
    for definition, limits in iter_definitions(spec.agents):
        model = load_llm_profile(limits.model_env_prefix, fallback=chat_config.llm, environment=environment) if limits.model_env_prefix else chat_config.llm
        config = {**plain(definition), "model": model.model, "budget": plain(limits)}
        custom = next((item for item in spec.agents.custom if item.name == definition.name), None)
        override = spec.agents.overrides.get(definition.name)
        config["role_prompt_source"] = (custom.role_prompt_path if custom else override.role_prompt_path if override else None) or "组件目录内置角色提示词"
        origin = "自定义定义" if definition.name in custom_names else "预设目录与 BotSpec 覆盖"
        raw_custom = next((item for item in raw_agents.get("custom", []) if item["name"] == definition.name), None)
        raw_budget = (raw_custom.get("budget") or {}) if raw_custom else (raw_agents.get(definition.name) or {})
        field_sources = {key: origin for key in config}
        field_sources["model"] = model_sources(limits.model_env_prefix, "继承基础模型 · chat")["model"]
        budget_path = f"agents.custom[{definition.name}].budget" if raw_custom else "agents." + definition.name
        field_sources.update({"budget." + key: value for key, value in budget_sources(raw_budget, budget_path, limits,
            research_default=definition.name == "browser_reader" and not raw_custom).items()})
        add("subagent:" + definition.name, definition.name, config, usage=definition.summary,
            field_sources=field_sources, membership="custom" if definition.name in custom_names else "preset")
    for workflow in iter_workflows(spec.agents):
        add("workflow:" + workflow.name, workflow.name, plain(workflow), usage=workflow.summary,
            field_sources={key: "Workflow 目录" for key in plain(workflow)})
    prompts = entities["prompts:instance"]
    prompts.update(usage="机器人身份与表达风格。动态人格、记忆来自 Application，宿主安全与权限规则由运行时生成。",
                   field_sources={key: spec_source("prompts." + key) for key in prompts["config"]})
    for index, provider in enumerate(spec.agents.search_providers):
        config = plain(provider)
        config["endpoint"] = provider.endpoint or DEFAULT_PROVIDER_ENDPOINTS.get(provider.kind)
        credential_key = provider.credential_env if provider.credential_env is not None else DEFAULT_PROVIDER_CREDENTIAL_ENVS.get(provider.kind, "")
        config["credential_env"] = credential_key or None
        config["credential_configured"] = bool(environment.get(credential_key)) if credential_key else None
        source = f"BotSpec · agents.unified_search.providers[{index}]"
        declarations = (raw_agents.get("unified_search") or {}).get("providers") or []
        raw_provider = declarations[index] if index < len(declarations) else {}
        provider_entity = add("search:" + provider.id, provider.id, config,
            usage="进程内统一搜索来源；启用配置不代表外部服务连通。",
            field_sources={key: source + "." + key if key in raw_provider else "代码默认值" for key in config})
        if not provider.endpoint:
            provider_entity["field_sources"]["endpoint"] = "代码默认地址"
        provider_entity["field_sources"]["credential_env"] = source if provider.credential_env is not None else "代码默认凭据引用"
        provider_entity["field_sources"]["credential_configured"] = "实例环境 · " + credential_key if credential_key else "此来源不要求凭据"
    projection["backend"] = backend
    projection["model"] = code["model"] if slot == "code" else chat_config.llm.model
