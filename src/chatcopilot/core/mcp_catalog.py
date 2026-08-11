"""Shared MCP catalog used by bot-level MCP bindings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from chatcopilot.project import ENV_PREFIX

_DEFAULT_CATALOG_RESOURCE = "mcp_catalog.yaml"


@dataclass(frozen=True)
class McpCatalogEntry:
    """One approved MCP template that bots can bind by ``ref``."""

    id: str
    title: str = ""
    source: str = "catalog"
    source_url: str = ""
    risk: str = "readonly"
    restart_required: bool = True
    server: dict[str, Any] = field(default_factory=dict)
    env_examples: dict[str, str] = field(default_factory=dict)

    def as_proposal(self) -> dict[str, Any]:
        return {
            "proposal_id": self.id,
            "title": self.title or self.id,
            "source": self.source,
            "source_url": self.source_url,
            "risk": self.risk,
            "restart_required": self.restart_required,
            "server": dict(self.server),
            "env_examples": dict(self.env_examples),
            "can_approve": True,
        }


def load_mcp_catalog(
    *,
    use_env_override: bool = True,
    strict: bool = False,
) -> dict[str, McpCatalogEntry]:
    """Load the MCP catalog, optionally honoring the machine-local override."""

    data = _load_catalog_yaml(
        use_env_override=use_env_override,
        strict=strict,
    )
    raw_entries = data.get("servers", [])
    if not isinstance(raw_entries, list):
        if strict:
            raise ValueError("MCP catalog servers must be a list")
        return {}
    entries: dict[str, McpCatalogEntry] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            if strict:
                raise ValueError("MCP catalog entries must be mappings")
            continue
        entry_id = str(raw.get("id", "")).strip()
        server = raw.get("server", {})
        if not entry_id or not isinstance(server, dict):
            if strict:
                raise ValueError("MCP catalog entries require an id and server mapping")
            continue
        if strict and entry_id in entries:
            raise ValueError("MCP catalog entry ids must be unique")
        entries[entry_id] = McpCatalogEntry(
            id=entry_id,
            title=str(raw.get("title", "") or "").strip(),
            source=str(raw.get("source", "catalog") or "catalog").strip(),
            source_url=str(raw.get("source_url", "") or "").strip(),
            risk=str(raw.get("risk", server.get("risk", "readonly")) or "readonly").strip(),
            restart_required=_as_bool(raw.get("restart_required"), default=True),
            server=dict(server),
            env_examples=_str_map(raw.get("env_examples", {})),
        )
    return entries


def resolve_mcp_catalog_ref(ref: str) -> McpCatalogEntry | None:
    return load_mcp_catalog().get(ref)


def resolve_catalog_server(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Merge a bot binding with its catalog template.

    A binding like ``{ref: tavily-search, enabled: true}`` expands to the catalog
    server mapping, then bot-local fields override the template. Full legacy
    server mappings without ``ref`` are returned unchanged.
    """

    ref = str(raw.get("ref", "") or "").strip()
    if not ref:
        resolved = dict(raw)
        resolved.pop("catalog_ref", None)
        return resolved
    entry = resolve_mcp_catalog_ref(ref)
    if entry is None:
        return None
    merged = dict(entry.server)
    for key, value in raw.items():
        if key not in {"ref", "catalog_ref"}:
            merged[key] = value
    merged["catalog_ref"] = ref
    return merged


def _load_catalog_yaml(*, use_env_override: bool, strict: bool = False) -> dict[str, Any]:
    import yaml

    override = (
        os.environ.get(f"{ENV_PREFIX}_MCP_CATALOG", "").strip()
        if use_env_override
        else ""
    )
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return data
            if strict:
                raise ValueError("MCP catalog root must be a mapping")
            return {}

    resource = resources.files("chatcopilot.botspec").joinpath(_DEFAULT_CATALOG_RESOURCE)
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError("MCP catalog root must be a mapping")
    return {}


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key).strip()}


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "McpCatalogEntry",
    "load_mcp_catalog",
    "resolve_catalog_server",
    "resolve_mcp_catalog_ref",
]
