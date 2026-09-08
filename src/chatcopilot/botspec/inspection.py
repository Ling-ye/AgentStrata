"""Configuration-only Console projection; never materializes runtime clients."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.inspection import configuration_projection, fingerprint, plain, environment_values
from chatcopilot.core.mcp_catalog import resolve_catalog_server
from .loader import load_botspec, validate_botspec
from .model import BotSpec
from .skills import load_skill_index
from .rag import load_rag_source_configs
from .deployment_env import deployment_environment, exported_environment, runtime_environment_keys
from .runtime_env import resolve_runtime_environment, _source_root


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
            projection["entities"].append({"id": f"tool:{name}", "layer": "capability", "name": name,
                "configured": name not in spec.tools.hide, "loaded": None, "available": None,
                "connected": None, "refs": [f"pack:{pack}"],
                "config": {"pack": pack, "requires_role": tool.requires_role, "parameters": plain(tool.input_schema)}})
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
