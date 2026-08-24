"""Bot composition inventory: aggregate BotSpec, component catalog, and
instance MCP bindings into a single read-only snapshot.

Only reads bot-owned YAML files and stable component catalog DTOs — no runtime
Agent dependency.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from console.control import services
from console.control.discovery import repo_root
from console.control.instances import BotInstance
from console.control.yaml_io import load_yaml_or_empty

# Ensure src/ is importable so we can reach chatcopilot.* registries.
_SRC = str(repo_root() / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _bot_spec_path(inst: BotInstance) -> Path:
    raw = Path(inst.bot_spec)
    return raw if raw.is_absolute() else repo_root() / raw


# ---------------------------------------------------------------------------
# MCP services
# ---------------------------------------------------------------------------

def _mcp_catalog() -> dict[str, dict[str, Any]]:
    """Load the shared MCP catalog keyed by catalog id."""
    from chatcopilot.component_catalog import iter_mcp_catalog_entries

    return {
        ref: {
            "id": entry.id,
            "title": entry.title,
            "risk": entry.risk,
            "server": dict(entry.server),
            "env_examples": dict(entry.env_examples),
        }
        for ref, entry in iter_mcp_catalog_entries()
    }


def _bot_mcp_bindings(inst: BotInstance) -> list[dict[str, Any]]:
    """Resolve bot MCP bindings against the catalog + infra status."""
    bot_yaml = _bot_spec_path(inst)
    bot_data = load_yaml_or_empty(bot_yaml)
    tools = bot_data.get("tools") if isinstance(bot_data.get("tools"), dict) else {}
    mcp = tools.get("mcp") if isinstance(tools.get("mcp"), dict) else {}
    servers_rel = mcp.get("servers") if isinstance(mcp, dict) else None
    if not servers_rel:
        return []

    servers_path = bot_yaml.parent / str(servers_rel)
    servers_data = load_yaml_or_empty(servers_path)
    refs = servers_data.get("servers", [])
    if not isinstance(refs, list):
        return []

    catalog = _mcp_catalog()
    infra_status = {str(s.get("id")): s for s in services.all_services_status()}

    results: list[dict[str, Any]] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        enabled = item.get("enabled", True)
        catalog_entry = catalog.get(ref, {})
        server_cfg = catalog_entry.get("server", {}) if isinstance(catalog_entry.get("server"), dict) else {}

        infra_id = str(server_cfg.get("id") or "")
        infra = infra_status.get(infra_id, {})

        results.append({
            "ref": ref,
            "title": str(catalog_entry.get("title") or ref),
            "enabled": enabled is not False,
            "risk": str(server_cfg.get("risk") or catalog_entry.get("risk") or ""),
            "exposure": str(server_cfg.get("exposure") or ""),
            "allowed_subagents": list(server_cfg.get("allowed_subagents") or []),
            "transport": str(server_cfg.get("transport") or ""),
            "infra_service_id": infra_id or None,
            "infra_state": str(infra.get("state") or ""),
            "infra_color": str(infra.get("color") or "grey"),
        })
    return results


# ---------------------------------------------------------------------------
# Tool packs
# ---------------------------------------------------------------------------

_NAMESPACE_LABELS: dict[str, str] = {
    "chat": "会话能力",
    "feishu": "飞书工具",
    "windows_fs": "Windows 文件系统",
    "unity_codebase": "Unity 代码库",
}


def _bot_tool_packs(bot_data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (tool pack detail list, hidden tool list)."""
    from chatcopilot.component_catalog import get_tool_pack_entry

    tools_section = bot_data.get("tools") if isinstance(bot_data.get("tools"), dict) else {}
    include = tools_section.get("packs", []) if isinstance(tools_section, dict) else []
    exclude = tools_section.get("hide", []) if isinstance(tools_section, dict) else []

    results: list[dict[str, Any]] = []
    for cap_id in (include if isinstance(include, list) else []):
        cap_id = str(cap_id).strip()
        if not cap_id:
            continue
        entry = get_tool_pack_entry(cap_id)
        ns = cap_id.split(".")[0] if "." in cap_id else cap_id
        results.append({
            "id": cap_id,
            "namespace": ns,
            "label": _NAMESPACE_LABELS.get(ns, ns),
            "description": str(entry.description) if entry else "",
            "has_tools": bool(entry and entry.tool_modules),
            "has_prompts": bool(entry and entry.policy_module),
        })

    excluded = [str(t).strip() for t in (exclude if isinstance(exclude, list) else []) if str(t).strip()]
    return results, excluded


def _bot_tool_features(bot_data: dict[str, Any]) -> list[dict[str, Any]]:
    from chatcopilot.component_catalog import get_tool_feature_entry

    tools_section = bot_data.get("tools") if isinstance(bot_data.get("tools"), dict) else {}
    features = tools_section.get("features", []) if isinstance(tools_section, dict) else []
    results: list[dict[str, Any]] = []
    for feature_id in (features if isinstance(features, list) else []):
        feature_id = str(feature_id).strip()
        if not feature_id:
            continue
        entry = get_tool_feature_entry(feature_id)
        ns = feature_id.split(".")[0] if "." in feature_id else feature_id
        results.append({
            "id": feature_id,
            "namespace": ns,
            "label": _NAMESPACE_LABELS.get(ns, ns),
            "description": str(entry.description) if entry else "",
        })
    return results


# ---------------------------------------------------------------------------
# Subagents & workflows
# ---------------------------------------------------------------------------

def _bot_agent_presets(bot_data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (subagent info list, workflow name list)."""
    from chatcopilot.component_catalog import BUILTIN_SUBAGENTS

    sa_section = bot_data.get("agents") if isinstance(bot_data.get("agents"), dict) else {}
    include = sa_section.get("presets", sa_section.get("include", [])) if isinstance(sa_section, dict) else []
    workflows = sa_section.get("workflows", []) if isinstance(sa_section, dict) else []

    results: list[dict[str, Any]] = []
    for name in (include if isinstance(include, list) else []):
        name = str(name).strip()
        preset = BUILTIN_SUBAGENTS.get(name)
        if not preset:
            results.append({"name": name, "tool_name": "", "kind": "", "summary": "", "workflow_tags": [], "budget": {}})
            continue

        budget_override = sa_section.get(name, {}) if isinstance(sa_section, dict) else {}
        budget = {}
        for field in ("max_model_turns", "max_tool_calls", "timeout_seconds", "max_output_chars"):
            val = budget_override.get(field) if isinstance(budget_override, dict) else None
            if val is not None:
                budget[field] = val

        results.append({
            "name": preset.name,
            "tool_name": preset.tool_name,
            "kind": preset.kind or "",
            "summary": preset.summary or "",
            "workflow_tags": list(preset.workflow_tags or ()),
            "budget": budget,
        })

    wf_list = [str(w).strip() for w in (workflows if isinstance(workflows, list) else []) if str(w).strip()]
    return results, wf_list


# ---------------------------------------------------------------------------
# Config overview
# ---------------------------------------------------------------------------

def _file_entry(base: Path, rel: str | None) -> dict[str, Any] | None:
    if not rel:
        return None
    path = base / rel
    return {"path": rel, "exists": path.is_file()}


def _bot_config(bot_data: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Extract config overview from BotSpec data."""
    prompts = bot_data.get("prompts") if isinstance(bot_data.get("prompts"), dict) else {}

    roles_raw = prompts.get("roles") if isinstance(prompts.get("roles"), dict) else {}
    roles: dict[str, Any] = {}
    for role_name, role_path in (roles_raw.items() if isinstance(roles_raw, dict) else []):
        roles[str(role_name)] = _file_entry(base_dir, str(role_path))

    context = bot_data.get("context") if isinstance(bot_data.get("context"), dict) else {}
    memory_raw = context.get("memory_store") if isinstance(context.get("memory_store"), dict) else {}
    memory = None
    if memory_raw:
        memory = {
            "provider": str(memory_raw.get("provider") or ""),
            "namespace": str(memory_raw.get("namespace") or ""),
            "schema": str(memory_raw.get("schema") or ""),
        }

    rag_raw = context.get("rag") if isinstance(context.get("rag"), dict) else {}
    rag = {"sources": str(rag_raw.get("sources") or "")} if rag_raw.get("sources") else None

    wiki_raw = context.get("wiki") if isinstance(context.get("wiki"), dict) else {}
    wiki = None
    if wiki_raw:
        wiki = {
            "enabled": bool(wiki_raw.get("enabled", False)),
            "root_env": str(wiki_raw.get("root_env") or "CHATCOPILOT_WIKI_ROOT"),
            "label": str(wiki_raw.get("label") or "wiki"),
            "read_role": str(wiki_raw.get("read_role") or "owner"),
            "private_chat_only": bool(wiki_raw.get("private_chat_only", True)),
        }

    codebases_raw = context.get("codebases") if isinstance(context.get("codebases"), dict) else {}
    codebases = {"registry": str(codebases_raw.get("registry") or "")} if codebases_raw.get("registry") else None

    skills_raw = context.get("playbooks") if isinstance(context.get("playbooks"), dict) else {}
    skills = {"manifest": str(skills_raw.get("manifest") or "")} if skills_raw.get("manifest") else None

    access_raw = bot_data.get("access") if isinstance(bot_data.get("access"), dict) else {}
    access = None
    if access_raw:
        access = {
            "owner_only_project_access": bool(
                access_raw.get("owner_only_project_access", False)
            ),
        }

    return {
        "persona": _file_entry(base_dir, prompts.get("persona")),
        "refusal": _file_entry(base_dir, prompts.get("refusal")),
        "safety": _file_entry(base_dir, prompts.get("safety")),
        "roles": roles if roles else None,
        "memory": memory,
        "rag": rag,
        "wiki": wiki,
        "codebases": codebases,
        "skills": skills,
        "access": access,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bot_inventory(inst: BotInstance) -> dict[str, Any]:
    """Full tool composition inventory for a bot instance."""
    bot_yaml = _bot_spec_path(inst)
    bot_data = load_yaml_or_empty(bot_yaml)
    base_dir = bot_yaml.parent

    tool_packs, hidden_tools = _bot_tool_packs(bot_data)
    tool_features = _bot_tool_features(bot_data)
    agent_presets, workflows = _bot_agent_presets(bot_data)

    return {
        "instance_id": inst.instance_id,
        "display_name": inst.display_name,
        "platform": inst.platform,
        "mcp_services": _bot_mcp_bindings(inst),
        "tool_packs": tool_packs,
        "tool_features": tool_features,
        "hidden_tools": hidden_tools,
        "agent_presets": agent_presets,
        "workflows": workflows,
        "config": _bot_config(bot_data, base_dir),
    }
