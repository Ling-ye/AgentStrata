"""Configuration-only Console projection; never materializes runtime clients."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from chatcopilot.core.inspection import configuration_projection, fingerprint, plain
from chatcopilot.core.mcp_catalog import resolve_catalog_server
from .loader import load_botspec, validate_botspec
from .model import BotSpec
from .skills import load_skill_index


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
                                for issue in validate_botspec(spec)]
    for pack in spec.tools.packs:
        for tool in iter_tool_pack_tools(pack):
            name = tool.name
            projection["entities"].append({"id": f"tool:{name}", "layer": "capability", "name": name,
                "configured": name not in spec.tools.hide, "loaded": None, "available": None,
                "connected": None, "refs": [f"pack:{pack}"],
                "config": {"pack": pack, "requires_role": tool.requires_role, "parameters": plain(tool.input_schema)}})
    return projection
