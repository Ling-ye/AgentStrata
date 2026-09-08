"""Instance configuration values for private runtime and operator projections."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

LAYERS = (
    ("control", "部署与服务"), ("gateway", "Gateway 与协议"),
    ("channel", "Channel 与平台"), ("authorization", "身份与权限"),
    ("application", "应用与上下文"), ("agent", "Agent 与模型"),
    ("capability", "工具与插件"), ("botspec", "实例配置"),
    ("contracts", "协议与数据兼容"),
)


def plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: plain(getattr(value, item.name)) for item in fields(value)
                if item.name not in {"source_path", "handler", "role_prompt", "raw"}}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((plain(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()[:24]


def configuration_projection(
    spec: Any, *, mcp: Any = (), skills: Any = (), rag: Any = (),
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    data = plain(spec)
    values = environment or {}
    entities: list[dict[str, Any]] = []

    def add(layer: str, identity: str, name: str, config: Any, enabled: bool | None = True) -> None:
        entities.append({"id": identity, "layer": layer, "name": name,
                         "configured": enabled, "loaded": None, "connected": None,
                         "available": None, "config": plain(config)})

    add("control", "service:instance", "实例服务", data.get("deploy", {}))
    add("gateway", "gateway:instance", "Gateway", data.get("gateway"), bool(data.get("gateway")))
    for name, config in (data.get("channels") or {}).items():
        if config:
            add("channel", f"channel:{name}", name, config)
    add("channel", "platform:instance", "平台", data.get("platform", {}))
    access = dict(data.get("access") or {})
    for name in ("QQ_ALLOW_FROM", "QQ_ALLOW_GROUPS", "CHATCOPILOT_OWNERS", "CHATCOPILOT_ADMINS"):
        access[name] = values.get(name)
    add("authorization", "policy:instance", "权限策略", access)
    add("application", "workspace:instance", "工作区", data.get("workspace", {}))
    for name, config in (data.get("context") or {}).items():
        add("application", f"context:{name}", name, config,
            config.get("enabled", True) if isinstance(config, dict) else bool(config))
    for source in plain(rag):
        add("application", f"rag:{source.get('id', source.get('label', 'source'))}",
            source.get("label", source.get("id", "RAG")), source)
    agents = data.get("agents") or {}
    add("agent", "agent:main", "主 Agent", agents)
    llm = data.get("llm") or {}
    chat = llm.get("chat") or {"env_prefix": llm.get("env_prefix")}
    research = llm.get("research") or {"env_prefix": llm.get("research_env_prefix"),
                                       "model": llm.get("research_model"),
                                       "execution": llm.get("research_execution")}
    for slot, config in (("chat", chat), ("research", research), ("code", llm.get("code", {}))):
        add("agent", f"model-slot:{slot}", f"模型 · {slot}", config)
    add("agent", "prompts:instance", "提示词配置", data.get("prompts", {}))
    for name in agents.get("presets", agents.get("include", ())):
        add("agent", f"subagent:{name}", name, (agents.get("overrides") or {}).get(name, {}))
    for name in agents.get("workflows", ()):
        add("agent", f"workflow:{name}", name, {})
    providers = agents.get("search_providers") or agents.get("search", {}).get("providers", {})
    if isinstance(providers, list):
        providers = {item["id"]: item for item in providers}
    for name, config in providers.items():
        add("capability", f"search:{name}", name, config, config.get("enabled", True))
    tools = data.get("tools") or {}
    for name in tools.get("packs", ()):
        add("capability", f"pack:{name}", name, {"hidden_tools": tools.get("hide", [])})
    for name in tools.get("features", ()):
        add("capability", f"feature:{name}", name, {})
    for config in plain(mcp):
        name = config.get("id", config.get("ref", "mcp"))
        add("capability", f"mcp:{name}", name, config, config.get("enabled", True))
    for config in plain(skills):
        name = config.get("id", config.get("name", "skill"))
        add("capability", f"skill:{name}", name, config)
    add("botspec", "config:instance", "BotSpec", {"id": data.get("id"), "schema_version": data.get("schema_version"),
                                                     "display_name": data.get("display_name")})
    add("contracts", "protocol:versions", "数据版本", {
        "gateway": (data.get("gateway") or {}).get("protocol_version"),
        "prompts": (data.get("prompts") or {}).get("schema_version"), "observation": 1,
    })
    for entity in entities:
        entity["environment"] = environment_values(entity["config"], values)
    result = {"layers": [{"id": key, "name": name} for key, name in LAYERS], "entities": entities,
              "visibility": "operator", "environment_revision_version": 2,
              "environment_revision": fingerprint({"access": access, "environment": {
                  entity["id"]: entity["environment"] for entity in entities}})}
    return result


def environment_values(value: Any, values: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_env") and isinstance(item, str):
                if item:
                    result[item] = values.get(item)
            elif key.endswith("env_prefix") and isinstance(item, str) and item:
                result.update({name: raw for name, raw in values.items() if name.startswith(item + "_")})
            else:
                result.update(environment_values(item, values))
    elif isinstance(value, list):
        for item in value:
            result.update(environment_values(item, values))
    elif isinstance(value, str):
        for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)", value):
            name = match.group(1) or match.group(2)
            result[name] = values.get(name)
    return result
