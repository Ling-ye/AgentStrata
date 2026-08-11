"""BotSpec MCP server configuration helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from chatcopilot.core.mcp_catalog import resolve_catalog_server
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.botspec.model import BotSpec, ValidationIssue

_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable_http"}
_SUPPORTED_EXPOSURES = {"subagent", "main", "disabled"}
_SUPPORTED_RISKS = {"search", "readonly", "interactive", "write"}



def load_mcp_server_configs(spec: BotSpec) -> tuple[McpServerConfig, ...]:
    """Load resolved MCP server configs for runtime use."""

    path = spec.resolve_path(spec.tools.mcp.servers)
    if path is None or not path.is_file():
        return ()
    data = _load_yaml(path)
    servers = data.get("servers", [])
    if not isinstance(servers, list):
        return ()
    out: list[McpServerConfig] = []
    for item in servers:
        if not isinstance(item, dict):
            continue
        catalog_ref = str(item.get("ref", "") or "").strip()
        resolved = resolve_catalog_server(item)
        if resolved is None:
            continue
        if _has_missing_env_refs(resolved.get("env", {})) or _has_missing_env_refs(resolved.get("headers", {})):
            continue
        cfg = _parse_server(
            resolved,
            resolve_env=True,
            catalog_ref=catalog_ref,
        )
        if cfg is not None and cfg.enabled and cfg.exposure != "disabled":
            out.append(cfg)
    return tuple(out)


def validate_mcp_servers(spec: BotSpec) -> list[ValidationIssue]:
    """Validate the optional tools.mcp.servers YAML file."""

    path = spec.resolve_path(spec.tools.mcp.servers)
    if path is None or not path.is_file():
        return []
    issues: list[ValidationIssue] = []
    try:
        data = _load_yaml(path)
    except Exception as exc:  # noqa: BLE001
        return [ValidationIssue("error", f"tools.mcp.servers YAML 解析失败: {exc}", "tools.mcp.servers")]

    servers = data.get("servers", [])
    if not isinstance(servers, list):
        return [ValidationIssue("error", "tools.mcp.servers 顶层必须包含 servers 列表", "tools.mcp.servers")]

    seen: set[str] = set()
    for idx, item in enumerate(servers):
        field = f"mcp.servers[{idx}]"
        if not isinstance(item, dict):
            issues.append(ValidationIssue("error", "每个 MCP server 必须是 mapping", field))
            continue
        if "catalog_ref" in item:
            issues.append(
                ValidationIssue(
                    "error",
                    "catalog_ref 是 runtime 保留字段，只能由 ref 解析产生",
                    f"{field}.catalog_ref",
                )
            )
        ref = str(item.get("ref", "") or "").strip()
        resolved = resolve_catalog_server(item)
        if ref and resolved is None:
            issues.append(ValidationIssue("error", f"未知 MCP catalog ref: {ref}", f"{field}.ref"))
            continue
        check_item = resolved or item

        if "enabled" in check_item and not _is_boolean_value(check_item.get("enabled")):
            issues.append(
                ValidationIssue(
                    "error",
                    "enabled 必须是 boolean",
                    f"{field}.enabled",
                )
            )

        server_id = str(check_item.get("id", "")).strip()
        if not server_id:
            issues.append(ValidationIssue("error", "MCP server id 不能为空", f"{field}.id"))
        elif server_id in seen:
            issues.append(ValidationIssue("error", f"MCP server id 重复: {server_id}", f"{field}.id"))
        else:
            seen.add(server_id)

        transport = str(check_item.get("transport", "stdio")).strip() or "stdio"
        if transport not in _SUPPORTED_TRANSPORTS:
            issues.append(
                ValidationIssue(
                    "error",
                    f"MCP transport 仅支持 {', '.join(sorted(_SUPPORTED_TRANSPORTS))}",
                    f"{field}.transport",
                )
            )
            continue

        exposure = str(check_item.get("exposure", "subagent")).strip() or "subagent"
        if exposure not in _SUPPORTED_EXPOSURES:
            issues.append(
                ValidationIssue(
                    "error",
                    f"MCP exposure 仅支持 {', '.join(sorted(_SUPPORTED_EXPOSURES))}",
                    f"{field}.exposure",
                )
            )

        risk = str(check_item.get("risk", "search")).strip() or "search"
        if risk not in _SUPPORTED_RISKS:
            issues.append(
                ValidationIssue(
                    "error",
                    f"MCP risk 仅支持 {', '.join(sorted(_SUPPORTED_RISKS))}",
                    f"{field}.risk",
                )
            )

        allowed_subagents = check_item.get("allowed_subagents", None)
        if allowed_subagents is not None:
            if not isinstance(allowed_subagents, list):
                issues.append(
                    ValidationIssue(
                        "error",
                        "allowed_subagents 必须是字符串列表",
                        f"{field}.allowed_subagents",
                    )
                )
            else:
                for value in allowed_subagents:
                    if not str(value).strip():
                        issues.append(
                            ValidationIssue(
                                "error",
                                "allowed_subagents 不能包含空值",
                                f"{field}.allowed_subagents",
                            )
                        )

        for list_name in (
            "allowed_tools",
            "denied_tools",
            "search_only_tools",
            "preferred_domains",
            "excluded_domains",
        ):
            raw_values = check_item.get(list_name)
            if raw_values is None:
                continue
            if not isinstance(raw_values, list):
                issues.append(
                    ValidationIssue(
                        "error",
                        f"{list_name} 必须是字符串列表",
                        f"{field}.{list_name}",
                    )
                )
                continue
            if any(not str(value).strip() for value in raw_values):
                issues.append(
                    ValidationIssue(
                        "error",
                        f"{list_name} 不能包含空值",
                        f"{field}.{list_name}",
                    )
                )

        raw_concurrency = check_item.get("max_concurrency")
        if raw_concurrency is not None:
            try:
                if int(float(raw_concurrency)) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                issues.append(
                    ValidationIssue(
                        "error",
                        "max_concurrency 必须是非负整数",
                        f"{field}.max_concurrency",
                    )
                )

        if transport == "stdio" and not str(check_item.get("command", "")).strip():
            issues.append(ValidationIssue("error", "stdio MCP server 必须声明 command", f"{field}.command"))
        artifact_digest = str(check_item.get("artifact_digest", "") or "").strip().lower()
        if artifact_digest and transport != "stdio":
            issues.append(
                ValidationIssue(
                    "error",
                    "artifact_digest 仅适用于 stdio MCP server",
                    f"{field}.artifact_digest",
                )
            )
        if artifact_digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
            issues.append(
                ValidationIssue(
                    "error",
                    "artifact_digest 必须是 sha256:<64 lowercase hex>",
                    f"{field}.artifact_digest",
                )
            )
        if transport in {"sse", "streamable_http"}:
            url = str(check_item.get("url", "")).strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                issues.append(
                    ValidationIssue("error", "HTTP MCP server url 必须是 http/https", f"{field}.url")
                )

        for map_name in ("env", "headers"):
            raw_map = check_item.get(map_name, {})
            if raw_map is None:
                continue
            if not isinstance(raw_map, dict):
                issues.append(ValidationIssue("error", f"{map_name} 必须是 mapping", f"{field}.{map_name}"))
                continue
            for key, value in raw_map.items():
                if not str(key).strip():
                    issues.append(ValidationIssue("error", f"{map_name} key 不能为空", f"{field}.{map_name}"))
                if not _is_env_ref(value):
                    issues.append(
                        ValidationIssue(
                            "error",
                            f"{map_name}.{key} 必须使用 ${{ENV_NAME}} 引用环境变量，不能写明文 secret",
                            f"{field}.{map_name}",
                        )
                    )
    return issues


def _parse_server(
    raw: dict[str, Any],
    *,
    resolve_env: bool,
    catalog_ref: str = "",
) -> McpServerConfig | None:
    server_id = str(raw.get("id", "")).strip()
    if not server_id:
        return None
    transport = str(raw.get("transport", "stdio")).strip() or "stdio"
    enabled = _as_bool(raw.get("enabled"), default=True)
    risk = str(raw.get("risk", "search")).strip() or "search"
    exposure = str(raw.get("exposure", "subagent")).strip() or "subagent"
    if exposure not in _SUPPORTED_EXPOSURES or risk not in _SUPPORTED_RISKS:
        return None
    env = _mapping_from_env_refs(raw.get("env", {}), resolve_env=resolve_env)
    headers = _mapping_from_env_refs(raw.get("headers", {}), resolve_env=resolve_env)
    return McpServerConfig(
        id=server_id,
        catalog_ref=catalog_ref,
        transport=transport,
        enabled=enabled,
        command=_optional_str(raw.get("command")),
        args=tuple(str(value) for value in _list(raw.get("args", []))),
        url=_optional_str(raw.get("url")),
        env=env,
        headers=headers,
        cwd=_optional_str(raw.get("cwd")),
        artifact_digest=str(raw.get("artifact_digest", "") or "").strip().lower(),
        tool_prefix=str(raw.get("tool_prefix", "") or ""),
        exposure=exposure,
        allowed_subagents=_allowed_subagents(raw.get("allowed_subagents"), risk=risk),
        allowed_tools=_string_tuple(raw.get("allowed_tools")),
        denied_tools=_string_tuple(raw.get("denied_tools")),
        risk=risk,
        timeout_seconds=_as_float(raw.get("timeout_seconds"), default=30.0),
        max_result_chars=max(1000, int(_as_float(raw.get("max_result_chars"), default=20000))),
        retry_on_timeout=_as_bool(raw.get("retry_on_timeout"), default=False),
        max_concurrency=max(0, int(_as_float(raw.get("max_concurrency"), default=0))),
        stateless_http=_as_bool(raw.get("stateless_http"), default=False),
        search_summary=str(raw.get("search_summary", "") or "").strip(),
        search_only_tools=_string_tuple(raw.get("search_only_tools")),
        preferred_domains=_string_tuple(raw.get("preferred_domains")),
        excluded_domains=_string_tuple(raw.get("excluded_domains")),
        search_domain_guidance=str(raw.get("search_domain_guidance", "") or "").strip(),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _mapping_from_env_refs(value: Any, *, resolve_env: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        match = _ENV_REF_RE.match(str(raw).strip())
        if not match:
            continue
        env_name = match.group(1)
        if resolve_env:
            env_value = os.environ.get(env_name)
            if env_value is None:
                continue
            out[str(key)] = env_value
        else:
            out[str(key)] = str(raw)
    return out


def _has_missing_env_refs(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for raw in value.values():
        match = _ENV_REF_RE.match(str(raw).strip())
        if match and os.environ.get(match.group(1)) is None:
            return True
    return False


def _is_env_ref(value: Any) -> bool:
    return bool(_ENV_REF_RE.match(str(value).strip()))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _allowed_subagents(value: Any, *, risk: str) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if risk == "readonly":
        return ("mcp_query",)
    # risk=search: empty tuple — auto-generated search subagents use convention matching
    return ()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_boolean_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "0",
        "false",
        "no",
        "off",
    }


def _as_float(value: Any, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["McpServerConfig", "load_mcp_server_configs", "validate_mcp_servers"]
