"""Configuration-only Console projection; never materializes runtime clients."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.inspection import fingerprint, plain
from chatcopilot.core.mcp_catalog import resolve_catalog_server
from .loader import load_botspec, validate_botspec
from .model import BotSpec
from .skills import load_skill_index
from .rag import load_rag_source_configs
from .deployment_env import deployment_environment, exported_environment, runtime_environment_keys
from .runtime_env import resolve_runtime_environment, _source_root


LAYERS = (
    ("control", "部署与服务"), ("gateway", "Gateway 与协议"),
    ("channel", "Channel 与平台"), ("authorization", "身份与权限"),
    ("application", "应用与上下文"), ("agent", "Agent 与模型"),
    ("capability", "工具与插件"), ("botspec", "实例配置"),
    ("contracts", "协议与数据兼容"),
)


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
    access = {
        "policy_version": "runtime-access-v2",
        "owner": "当前实例资源和已配置项目",
        "member": "公共查询、当前会话文件、记忆读取和追加",
    }
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


def source_revision(spec: BotSpec) -> str:
    references = [spec.prompts.identity, spec.prompts.response_style, spec.prompts.refusal_style,
                  *spec.prompts.role_styles.values(), *spec.prompts.mode_styles.values(),
                  spec.tools.mcp.servers, spec.context.rag.sources, spec.context.playbooks.manifest]
    digests = {}
    for reference in references:
        path = spec.resolve_path(reference)
        if path and path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
            digests[str(reference)] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif reference:
            digests[str(reference)] = "unavailable"
    return fingerprint({"spec": plain(spec), "references": digests})


def declared_configuration(path: Path, environment: Mapping[str, str]) -> dict[str, Any]:
    from chatcopilot.component_catalog import iter_tool_pack_tools
    import yaml

    spec = load_botspec(path)
    mcp_path = spec.resolve_path(spec.tools.mcp.servers)
    mcp = []
    if mcp_path and mcp_path.is_file():
        raw = yaml.safe_load(mcp_path.read_text(encoding="utf-8")) or {}
        for item in raw.get("servers", []):
            resolved = resolve_catalog_server(item)
            mcp.append(resolved or item)
    manifest = spec.resolve_path(spec.context.playbooks.manifest)
    skills = load_skill_index(manifest) if manifest and manifest.is_file() else ()
    projection = configuration_projection(spec, mcp=mcp, skills=skills, environment=environment)
    projection["configuration_revision"] = source_revision(spec)
    projection["backend"] = spec.agents.backend
    projection["validation"] = [{"field": issue.field, "level": issue.level, "message": issue.message}
                                for issue in validate_botspec(spec, environment=dict(environment))]
    for pack in spec.tools.packs:
        for tool in iter_tool_pack_tools(pack):
            name = tool.name
            projection["entities"].append(
                {
                    "id": f"tool:{name}",
                    "layer": "capability",
                    "name": name,
                    "configured": name not in spec.tools.hide,
                    "loaded": None,
                    "available": None,
                    "connected": None,
                    "refs": [f"pack:{pack}"],
                    "config": {
                        "pack": pack,
                        "access": tool.access,
                        "parameters": plain(tool.input_schema),
                    },
                }
            )
    _context_details(spec, projection, environment)
    return projection


def expected_configuration(path: Path, saved: Mapping[str, str], *, home: Path) -> dict[str, Any]:
    spec = load_botspec(path)
    source_root = _source_root(path)
    provisioned = deployment_environment(spec, saved, source_root=source_root, home=home)
    exported = exported_environment(provisioned, runtime_environment_keys(spec))
    effective = resolve_runtime_environment(spec, exported, source_root=source_root)
    current = declared_configuration(path, saved)
    expected = declared_configuration(path, effective)
    current["effective_environment_revision"] = expected["environment_revision"]
    current["reference_revision"] = expected["reference_revision"]
    raw_entities = {item["id"]: item for item in current["entities"]}
    effective_entities = {item["id"]: item for item in expected["entities"]}
    current["entities"] = [raw_entities.get(item["id"], {**item}) for item in expected["entities"]]
    for item in current["entities"]:
        resolved = effective_entities.get(item["id"], {})
        item["effective_environment"] = resolved.get("environment", {})
        item["effective_config"] = resolved.get("config")
    by_id = {item["id"]: item for item in current["entities"]}
    service = by_id["service:instance"]["effective_config"]
    service.update({"instance_id": provisioned["CHATCOPILOT_INSTANCE_ID"], "wsl_home": provisioned["CHATCOPILOT_HOME"], "env_file": provisioned["CHATCOPILOT_ENV_FILE"],
                    "workspace_root": provisioned["CHATCOPILOT_WORKSPACE_ROOT"], "log_dir": provisioned["CHATCOPILOT_LOG_DIR"],
                    "source_spec": str(path), "source_root": str(source_root)})
    code = by_id["model-slot:code"]
    prefix = spec.llm.env_prefix + "_CODE_"
    code["effective_environment"] = {key: value for key, value in effective.items() if key.startswith(prefix)}
    code_config = code["effective_config"]
    for field in ("model", "reasoning_effort", "provider", "command", "timeout_seconds"):
        if prefix + field.upper() in effective:
            code_config[field] = effective[prefix + field.upper()]
    current["validation"] = expected["validation"]
    return current


def _context_details(spec: BotSpec, projection: dict[str, Any], environment: Mapping[str, str]) -> None:
    import yaml
    from chatcopilot.external_tools.codebase.config import load_registry

    def add(identity, name, config, environment_values=None):
        projection["entities"].append({"id": identity, "layer": "application", "name": name,
            "configured": True, "loaded": None, "connected": None, "available": None,
            "config": plain(config), "environment": environment_values or {}})

    references = {}
    reference_environment = {}
    for kind, reference in (("rag", spec.context.rag.sources), ("codebase", spec.context.codebases.registry)):
        path = spec.resolve_path(reference)
        if not path or not path.is_file():
            continue
        if path.stat().st_size > 8 * 1024 * 1024:
            projection["validation"].append({"field": kind, "level": "error", "message": "配置引用超过读取上限"})
            references[kind] = "truncated"
            continue
        payload = path.read_bytes()
        references[kind] = hashlib.sha256(payload).hexdigest()
        raw = yaml.safe_load(payload) or {}
        reference_environment[kind] = environment_values(raw, environment)
        try:
            if kind == "rag":
                sources = load_rag_source_configs(spec, environment=environment)
                resolved = {source.label: source for source in sources}
                for index, source in enumerate(raw.get("sources", [])):
                    item = source if isinstance(source, dict) else {"path": source}
                    label = str(item.get("label") or item.get("path") or index)
                    config = plain(resolved[label]) if label in resolved else {**item, "resolution": "未解析：请检查环境引用"}
                    add(f"rag:{label}", label, config)
            else:
                registry = load_registry(path, environment=environment)
                for key, entry in registry.repositories.items():
                    add(f"codebase:{key}", entry.display_name, entry)
        except (ValueError, OSError, TypeError) as exc:
            add(f"context:{kind}-details", "RAG 数据源" if kind == "rag" else "代码仓库条目",
                {"declarations": raw, "error": str(exc)})
    for entity in projection["entities"]:
        if entity["id"].startswith("skill:"):
            body = entity.get("config", {}).get("body_path")
            path = Path(body) if body else None
            references[entity["id"]] = hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() and path.stat().st_size <= 8 * 1024 * 1024 else "unavailable"
    projection["reference_revision"] = fingerprint({"files": references, "environment": reference_environment})
