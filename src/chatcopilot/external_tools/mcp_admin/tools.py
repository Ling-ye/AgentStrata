"""Owner-only helpers for discovering and enabling curated MCP servers."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.core.bot_paths import resolve_bot_spec_path
from chatcopilot.core.mcp_catalog import load_mcp_catalog, resolve_catalog_server
from chatcopilot.core.mcp_probe import probe_mcp_server
from chatcopilot.core.settings import get_bot_spec_env
from chatcopilot.external_tools.shared.tool_spec import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)
from chatcopilot.project import ENV_PREFIX

_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"
_REGISTRY_PAGE_LIMIT = 100
_REGISTRY_DEFAULT_MAX_PAGES = 5
_MCP_ADMIN_RESULT_SCHEMA = {"type": "object", "additionalProperties": True}


def _payload_result(payload: dict[str, Any], *, summary: str) -> ToolResult:
    ok = payload.get("ok") is not False
    error = str(payload.get("message") or payload.get("error") or summary)
    return ToolResult(
        ok=ok,
        summary=summary if ok else "",
        data=payload,
        error=None if ok else error,
        error_code=str(payload.get("error_code") or payload.get("error") or "mcp_admin_failed")
        if not ok
        else "",
        stage="execution" if not ok else "",
    )


def _handler_discover(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    query = str((args or {}).get("query") or "").strip()
    if not query:
        raise ValueError("query must not be empty")

    proposals = _curated_matches(query)
    max_pages = max(
        1,
        min(int((args or {}).get("registry_max_pages") or _REGISTRY_DEFAULT_MAX_PAGES), 20),
    )
    registry_result = _registry_matches(query, max_pages=max_pages)
    proposals.extend(registry_result.proposals)
    if not proposals:
        proposals.append(
            {
                "proposal_id": "manual-review-required",
                "title": f"No curated MCP candidate matched: {query}",
                "source": "manual_review",
                "risk": "readonly",
                "restart_required": True,
                "can_approve": False,
                "notes": [
                    "Review the official source, license, launch command, secrets, and "
                    "remote write behavior before adding a catalog entry or a manual binding.",
                    "AgentStrata does not install third-party MCP servers automatically.",
                ],
            }
        )
    payload = {
        "ok": not bool(registry_result.error_code) or bool(proposals),
        "query": query,
        "proposals": proposals,
        "registry": {
            "api": "v0.1",
            "pages_fetched": registry_result.pages_fetched,
            "pagination_exhausted": registry_result.pagination_exhausted,
            "error_code": registry_result.error_code,
            "error": registry_result.error,
        },
    }
    return _payload_result(
        payload,
        summary=f"发现 {len(proposals)} 个 MCP 候选。",
    )


def _handler_approve(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    proposal_id = str((args or {}).get("proposal_id") or "").strip()
    bot_value = str((args or {}).get("bot") or "").strip() or None
    if not proposal_id:
        raise ValueError("proposal_id must not be empty")

    catalog_entry = load_mcp_catalog().get(proposal_id)
    if catalog_entry is None:
        return _payload_result(
            {
                "ok": False,
                "error": "catalog_entry_required",
                "proposal_id": proposal_id,
                "message": (
                    "Only reviewed built-in catalog entries can be enabled by this tool. "
                    "Review other servers manually and add an explicit BotSpec binding."
                ),
            },
            summary="MCP catalog entry is required.",
        )

    spec = _load_mcp_admin_bot_spec(_resolve_bot_path(bot_value))
    if not spec.mcp_servers:
        raise ValueError("BotSpec does not declare tools.mcp.servers")
    servers_path = spec.resolve_path(spec.mcp_servers)
    if servers_path is None:
        raise ValueError("Unable to resolve tools.mcp.servers")
    servers_path.parent.mkdir(parents=True, exist_ok=True)
    binding = {"ref": catalog_entry.id, "enabled": True}
    changed = _upsert_server(servers_path, binding)
    example_changed = _append_local_env_examples(
        spec.base_dir / "local.env.example",
        catalog_entry.env_examples,
    )
    return _payload_result(
        {
            "ok": True,
            "proposal_id": proposal_id,
            "bot": spec.id,
            "servers_path": str(servers_path),
            "servers_yaml_changed": changed,
            "local_env_example_changed": example_changed,
            "restart_required": True,
            "next_step": (
                "Set required secrets in bots/<id>/local.env, then apply the BotSpec "
                "through the existing update and restart flow."
            ),
        },
        summary=f"已为 {spec.id} 启用 MCP catalog entry {proposal_id}。",
    )


def _handler_list(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    bot_value = str((args or {}).get("bot") or "").strip() or None
    spec = _load_mcp_admin_bot_spec(_resolve_bot_path(bot_value))
    servers_path = spec.resolve_path(spec.mcp_servers)
    if servers_path is None or not servers_path.is_file():
        return _payload_result(
            {"ok": True, "bot": spec.id, "servers": []},
            summary=f"{spec.id} 当前没有 MCP bindings。",
        )
    data = _load_servers_yaml(servers_path)
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        servers = []
    visible: list[dict[str, Any]] = []
    for item in servers:
        if not isinstance(item, dict):
            continue
        resolved = resolve_catalog_server(item) or item
        visible.append(
            {
                "ref": item.get("ref"),
                "id": resolved.get("id"),
                "enabled": resolved.get("enabled", True),
                "transport": resolved.get("transport", "stdio"),
                "exposure": resolved.get("exposure", "subagent"),
                "allowed_subagents": resolved.get(
                    "allowed_subagents",
                    [] if resolved.get("risk", "search") == "search" else ["mcp_query"],
                ),
                "allowed_tools": resolved.get("allowed_tools", []),
                "denied_tools": resolved.get("denied_tools", []),
                "risk": resolved.get("risk", "search"),
                "tool_prefix": resolved.get("tool_prefix", ""),
            }
        )
    return _payload_result(
        {"ok": True, "bot": spec.id, "servers": visible},
        summary=f"{spec.id} 当前有 {len(visible)} 个 MCP bindings。",
    )


def _handler_probe(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    server_id = str((args or {}).get("server_id") or "").strip()
    bot_value = str((args or {}).get("bot") or "").strip() or None
    if not server_id:
        raise ValueError("server_id must not be empty")

    spec = _load_mcp_admin_bot_spec(_resolve_bot_path(bot_value))
    servers_path = spec.resolve_path(spec.mcp_servers)
    if servers_path is None or not servers_path.is_file():
        raise ValueError("BotSpec does not declare an MCP servers file")
    bound, resolved = _find_bound_server(servers_path, server_id)
    if resolved is None:
        raise ValueError(f"MCP server binding not found: {server_id}")

    result = probe_mcp_server(
        _server_config_for_probe(resolved),
        allow_private_network=True,
    )
    payload = result.to_dict()
    payload.update(
        {
            "bot": spec.id,
            "binding": _server_key(bound or resolved),
            "read_only": True,
        }
    )
    return _payload_result(payload, summary=f"MCP server {server_id} 探针完成。")


def _curated_matches(query: str) -> list[dict[str, Any]]:
    needle = query.casefold()
    out: list[dict[str, Any]] = []
    for entry in load_mcp_catalog().values():
        proposal = entry.as_proposal()
        haystack = " ".join(
            str(proposal.get(key, ""))
            for key in ("proposal_id", "title", "source_url", "risk")
        ).casefold()
        if any(part and part in haystack for part in re.split(r"\s+", needle)):
            out.append(proposal)
    return out


@dataclass(frozen=True)
class _RegistryDiscoveryResult:
    proposals: tuple[dict[str, Any], ...] = ()
    pages_fetched: int = 0
    pagination_exhausted: bool = False
    error_code: str = ""
    error: str = ""


def _registry_matches(query: str, *, max_pages: int) -> _RegistryDiscoveryResult:
    needles = tuple(part for part in re.split(r"\s+", query.casefold()) if part)
    out: list[dict[str, Any]] = []
    cursor = ""
    pages = 0
    try:
        while pages < max_pages:
            params = {"limit": str(_REGISTRY_PAGE_LIMIT)}
            if cursor:
                params["cursor"] = cursor
            payload = _fetch_registry_page(
                _REGISTRY_URL + "?" + urllib.parse.urlencode(params)
            )
            servers = payload.get("servers")
            metadata = payload.get("metadata", {})
            if not isinstance(servers, list) or not isinstance(metadata, dict):
                raise ValueError("registry response requires servers[] and metadata{}")
            pages += 1
            for entry in servers:
                server = entry.get("server") if isinstance(entry, dict) else None
                meta = entry.get("_meta", {}) if isinstance(entry, dict) else {}
                if not isinstance(server, dict):
                    continue
                official = (
                    meta.get("io.modelcontextprotocol.registry/official", {})
                    if isinstance(meta, dict)
                    else {}
                )
                if isinstance(official, dict) and official.get("isLatest") is False:
                    continue
                status = str(
                    server.get("status")
                    or (official.get("status") if isinstance(official, dict) else "")
                    or "active"
                ).lower()
                if status == "deleted":
                    continue
                text = " ".join(
                    str(server.get(key, ""))
                    for key in ("name", "title", "description", "websiteUrl", "version")
                ).casefold()
                if needles and not all(part in text for part in needles):
                    continue
                proposal = _proposal_from_registry_server(server)
                proposal["registry_status"] = status
                if status != "active":
                    proposal["can_approve"] = False
                out.append(proposal)
                if len(out) >= 5:
                    return _RegistryDiscoveryResult(
                        proposals=tuple(out),
                        pages_fetched=pages,
                        pagination_exhausted=False,
                    )
            cursor = str(metadata.get("nextCursor") or "").strip()
            if not cursor:
                return _RegistryDiscoveryResult(
                    proposals=tuple(out),
                    pages_fetched=pages,
                    pagination_exhausted=True,
                )
        return _RegistryDiscoveryResult(
            proposals=tuple(out),
            pages_fetched=pages,
            pagination_exhausted=False,
        )
    except json.JSONDecodeError as exc:
        return _RegistryDiscoveryResult(
            proposals=tuple(out),
            pages_fetched=pages,
            error_code="registry_invalid_json",
            error=f"{type(exc).__name__}: {exc}",
        )
    except (OSError, TimeoutError) as exc:
        return _RegistryDiscoveryResult(
            proposals=tuple(out),
            pages_fetched=pages,
            error_code="registry_unavailable",
            error=f"{type(exc).__name__}: {exc}",
        )
    except (TypeError, ValueError) as exc:
        return _RegistryDiscoveryResult(
            proposals=tuple(out),
            pages_fetched=pages,
            error_code="registry_invalid_schema",
            error=f"{type(exc).__name__}: {exc}",
        )


def _fetch_registry_page(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "AgentStrata/0.1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("registry response must be a JSON object")
    return payload


def _proposal_from_registry_server(server: dict[str, Any]) -> dict[str, Any]:
    remotes = server.get("remotes") if isinstance(server.get("remotes"), list) else []
    packages = server.get("packages") if isinstance(server.get("packages"), list) else []
    repository = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    proposal: dict[str, Any] = {
        "proposal_id": "registry-"
        + _safe_id(str(server.get("name") or server.get("title") or "mcp")),
        "title": server.get("title") or server.get("name"),
        "description": server.get("description", ""),
        "version": server.get("version", ""),
        "source": "official_mcp_registry",
        "source_url": repository.get("url") or server.get("websiteUrl") or "",
        "risk": _guess_risk(str(server.get("description", ""))),
        "restart_required": True,
        "can_approve": False,
        "notes": [
            "Registry discovery is read-only. Review and configure this server manually; "
            "AgentStrata does not install registry candidates automatically."
        ],
        "declared_environment_variables": [
            str(item.get("name") or "")
            for package in packages
            if isinstance(package, dict)
            for item in (package.get("environmentVariables") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ],
    }
    if remotes:
        remote = remotes[0]
        if isinstance(remote, dict):
            proposal["suggested_transport"] = _registry_transport(remote.get("type"))
            proposal["suggested_url"] = remote.get("url", "")
    elif packages:
        package = packages[0]
        if isinstance(package, dict):
            proposal["suggested_package"] = {
                "registry": package.get("registryType", ""),
                "identifier": package.get("identifier", ""),
                "version": package.get("version", ""),
            }
    return proposal


@dataclass(frozen=True)
class _McpAdminBotSpec:
    id: str
    path: Path
    mcp_servers: str

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve_path(self, value: str | None) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_absolute() else self.base_dir / path


def _load_mcp_admin_bot_spec(path: Path) -> _McpAdminBotSpec:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"BotSpec top level must be a mapping: {path}")
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    mcp = tools.get("mcp") if isinstance(tools.get("mcp"), dict) else {}
    return _McpAdminBotSpec(
        id=str(data.get("id") or path.parent.name),
        path=path,
        mcp_servers=str(mcp.get("servers") or "").strip(),
    )


def _resolve_bot_path(value: str | None) -> Path:
    if value:
        return resolve_bot_spec_path(value)
    source_path = os.environ.get(f"{ENV_PREFIX}_SOURCE_BOT_SPEC", "").strip()
    if source_path:
        path = Path(source_path).expanduser().resolve()
        if path.is_file():
            return path
    env_path = get_bot_spec_env()
    if env_path is not None:
        return env_path
    bot_id = os.environ.get(f"{ENV_PREFIX}_BOT_ID", "").strip()
    if bot_id:
        return resolve_bot_spec_path(bot_id)
    raise RuntimeError(
        "Unable to locate the BotSpec; pass bot or set CHATCOPILOT_SOURCE_BOT_SPEC, "
        "CHATCOPILOT_BOT_SPEC, or CHATCOPILOT_BOT_ID."
    )


def _load_servers_yaml(path: Path) -> dict[str, Any]:
    import yaml

    if not path.is_file():
        return {"servers": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {"servers": []}


def _write_servers_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _upsert_server(path: Path, server: dict[str, Any]) -> bool:
    data = _load_servers_yaml(path)
    servers = data.get("servers")
    if not isinstance(servers, list):
        servers = []
        data["servers"] = servers
    server_key = _server_key(server)
    for index, item in enumerate(servers):
        if isinstance(item, dict) and _server_key(item) == server_key:
            if item == server:
                return False
            servers[index] = dict(server)
            _write_servers_yaml(path, data)
            return True
    servers.append(dict(server))
    _write_servers_yaml(path, data)
    return True


def _server_key(server: dict[str, Any]) -> str:
    ref = str(server.get("ref", "") or "").strip()
    if ref:
        return f"ref:{ref}"
    return f"id:{str(server.get('id', '') or '').strip()}"


def _find_bound_server(
    path: Path,
    server_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    data = _load_servers_yaml(path)
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        return None, None
    for item in servers:
        if not isinstance(item, dict):
            continue
        resolved = resolve_catalog_server(item)
        if resolved is None:
            continue
        candidates = {
            str(item.get("ref") or "").strip(),
            str(item.get("id") or "").strip(),
            str(resolved.get("id") or "").strip(),
        }
        if server_id in candidates:
            return dict(item), resolved
    return None, None


def _server_config_for_probe(server: dict[str, Any]) -> McpServerConfig:
    env = _resolve_probe_secret_refs(server.get("env", {}), "env")
    headers = _resolve_probe_secret_refs(server.get("headers", {}), "headers")
    return McpServerConfig(
        id=str(server.get("id") or "").strip(),
        transport=str(server.get("transport") or "stdio").strip() or "stdio",
        command=str(server.get("command") or "").strip() or None,
        args=tuple(str(item) for item in (server.get("args") or [])),
        url=str(server.get("url") or "").strip() or None,
        env=env,
        headers=headers,
        cwd=str(server.get("cwd") or "").strip() or None,
        artifact_digest=str(server.get("artifact_digest") or "").strip().lower(),
        risk=str(server.get("risk") or "readonly").strip() or "readonly",
        allowed_tools=tuple(str(item) for item in (server.get("allowed_tools") or [])),
        timeout_seconds=float(server.get("timeout_seconds") or 30),
        max_result_chars=int(server.get("max_result_chars") or 20000),
        stateless_http=_as_bool(server.get("stateless_http"), default=False),
    )


def _resolve_probe_secret_refs(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"server.{field} must be a mapping")
    resolved: dict[str, str] = {}
    for key, raw in value.items():
        name = str(key).strip()
        if not name:
            raise ValueError(f"server.{field} key must not be empty")
        if field == "env" and name in {"HOME", "PATH", "TMPDIR"}:
            raise ValueError(f"server.env may not override probe isolation variable: {name}")
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(raw).strip())
        if not match:
            raise ValueError(f"server.{field}.{name} must use an environment reference")
        env_name = match.group(1)
        if env_name not in os.environ:
            raise ValueError(f"required probe environment variable is missing: {env_name}")
        resolved[name] = os.environ[env_name]
    return resolved


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _append_local_env_examples(path: Path, examples: dict[str, str]) -> bool:
    if not examples:
        return False
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    additions: list[str] = []
    for key, example in examples.items():
        if re.search(rf"^\s*(?:export\s+)?{re.escape(key)}=", content, re.MULTILINE):
            continue
        additions.append("# MCP: set locally when the matching server is enabled.")
        additions.append(f'# export {key}="{example}"')
    if not additions:
        return False
    separator = "" if content.endswith("\n") or not content else "\n"
    path.write_text(content + separator + "\n".join(additions) + "\n", encoding="utf-8")
    return True


def _safe_id(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return text or "mcp"


def _registry_transport(value: Any) -> str:
    raw = str(value or "").replace("-", "_")
    if raw == "streamable_http":
        return "streamable_http"
    if raw == "sse":
        return "sse"
    return "stdio"


def _guess_risk(description: str) -> str:
    text = description.casefold()
    if any(
        word in text
        for word in ("create", "update", "delete", "manage", "write", "send", "pay")
    ):
        return "write"
    if any(word in text for word in ("search", "browse", "query", "read", "docs")):
        return "readonly"
    return "readonly"


TOOLS = [
    ToolDef(
        name="discover_mcp_server",
        summary=(
            "Search the official MCP Registry and the built-in catalog. Discovery is "
            "read-only and never installs or enables a server. Owner only."
        ),
        input_schema=object_schema({
            "query": {
                "type": "string",
                "description": "MCP name or capability, such as github or docs search.",
            },
            "registry_max_pages": {
                "type": "integer",
                "description": "Registry v0.1 pages to inspect; default 5, maximum 20.",
            },
        }, required=("query",)),
        output_schema=_MCP_ADMIN_RESULT_SCHEMA,
        handler=_handler_discover,
        requires_role="owner",
        category="mcp.admin",
        owner="mcp_admin",
        module=__name__,
        artifact_kinds=(),
    ),
    ToolDef(
        name="approve_mcp_server",
        summary=(
            "Enable a reviewed built-in MCP catalog proposal in the current BotSpec. "
            "Unknown registry candidates require manual review and configuration. Owner only."
        ),
        input_schema=object_schema({
            "proposal_id": {
                "type": "string",
                "description": "An approvable proposal_id returned by discover_mcp_server.",
            },
            "bot": {
                "type": "string",
                "description": "Optional bot id or bot.yaml path.",
            },
            "server": {
                "type": "object",
                "description": (
                    "Legacy manual server proposal payload; retained only so the handler "
                    "can return the reviewed-catalog requirement explicitly."
                ),
                "additionalProperties": True,
            },
        }, required=("proposal_id",)),
        output_schema=_MCP_ADMIN_RESULT_SCHEMA,
        handler=_handler_approve,
        requires_role="owner",
        category="mcp.admin",
        owner="mcp_admin",
        module=__name__,
        artifact_kinds=(),
        metadata={"execution_boundary": "codex"},
    ),
    ToolDef(
        name="probe_mcp_server",
        summary=(
            "Initialize one existing BotSpec MCP binding and list its tool schemas without "
            "calling any remote tool or changing configuration. Owner only."
        ),
        input_schema=object_schema({
            "server_id": {
                "type": "string",
                "description": "Existing binding id or catalog ref from this BotSpec.",
            },
            "bot": {
                "type": "string",
                "description": "Optional bot id or bot.yaml path.",
            },
        }, required=("server_id",)),
        output_schema=_MCP_ADMIN_RESULT_SCHEMA,
        handler=_handler_probe,
        requires_role="owner",
        category="mcp.admin",
        owner="mcp_admin",
        module=__name__,
        artifact_kinds=(),
    ),
    ToolDef(
        name="list_mcp_servers",
        summary="List MCP bindings and exposure policies for the current bot. Owner only.",
        input_schema=object_schema({
            "bot": {
                "type": "string",
                "description": "Optional bot id or bot.yaml path.",
            }
        }),
        output_schema=_MCP_ADMIN_RESULT_SCHEMA,
        handler=_handler_list,
        requires_role="owner",
        category="mcp.admin",
        owner="mcp_admin",
        module=__name__,
        artifact_kinds=(),
    ),
]

TOOL_PROVIDER = static_tool_provider(
    "mcp-admin",
    packs={"mcp.admin": tuple(TOOLS)},
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
