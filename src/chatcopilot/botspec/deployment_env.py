"""Pure deployment environment projection shared by provisioning and inspection."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable, Mapping

from chatcopilot.botspec.runtime_env import llm_runtime_env_defaults
from chatcopilot.core.settings import expand_leading_home
from chatcopilot.core.mcp_catalog import resolve_catalog_server
from chatcopilot.platforms import registry as _registry

_CC_CONNECT_VERSION = "1.4.0-beta.3"


def deployment_environment(spec, local_env: Mapping[str, str], *, source_root: Path, home: Path, runtime_root: str = "") -> dict[str, str]:
    def expand_deploy_path(value):
        return expand_leading_home((value or "").strip(), home=home)

    deploy = spec.deploy
    local_env = {key: expand_leading_home(value, home=home) for key, value in local_env.items()}
    instance_id = deploy.instance_id or spec.id
    wsl_home = expand_deploy_path(deploy.wsl_home) or str(home / f"ChatCopilot-{instance_id}")
    workspace_root = expand_deploy_path(deploy.workspace_root) or str(
        home / "chatcopilot-workspaces" / instance_id
    )
    log_dir = expand_deploy_path(deploy.log_dir) or str(home / "chatcopilot-logs" / instance_id)
    env_file = expand_deploy_path(deploy.env_file) or str(home / f".chatcopilot-{instance_id}.env")
    qq_gateway = spec.channels.qq is not None
    cc_config_dir = ""
    if not qq_gateway:
        cc_config_dir = expand_deploy_path(deploy.cc_connect_config_dir) or str(
            home / ".chatcopilot-runtime" / instance_id / ".cc-connect"
        )
    try:
        bot_rel = spec.source_path.relative_to(source_root)
        runtime_bot_spec = str(Path(wsl_home) / bot_rel)
    except ValueError:
        runtime_bot_spec = str(spec.source_path)

    adapter = _registry.get_adapter(spec.platform.type)
    values = {
        item.env_key: item.default
        for item in adapter.required_secrets()
        if item.default is not None
    }
    values.update(_tool_pack_runtime_defaults(spec.tools.packs))
    values.update(llm_runtime_env_defaults(spec.llm))
    values.update(local_env)
    # Generic QQ_* export must not carry the removed admission setting forward.
    values.pop("QQ_ALLOW_GROUPS", None)
    values.update(
        {
            "CHATCOPILOT_INSTANCE_ID": instance_id,
            "CHATCOPILOT_HOME": wsl_home,
            "CHATCOPILOT_BOT_SPEC": runtime_bot_spec,
            "CHATCOPILOT_SOURCE_BOT_SPEC": str(spec.source_path),
            "CHATCOPILOT_ENV_FILE": env_file,
            "CHATCOPILOT_WORKSPACE_ROOT": workspace_root,
            "WORKSPACE_ROOT": workspace_root,
            "CHATCOPILOT_LOG_DIR": log_dir,
            "CHATCOPILOT_DISPLAY_NAME": spec.display_name,
        }
    )
    if qq_gateway:
        assert spec.gateway is not None
        gateway_port = str(values.get(spec.gateway.port_env, "18789") or "").strip()
        state_root = str(values.get(spec.gateway.state_root_env, "") or "").strip()
        if not state_root:
            state_root = str(
                home / ".local" / "state" / "agentstrata" / instance_id / "gateway"
            )
        values[spec.gateway.port_env] = gateway_port
        values[spec.gateway.state_root_env] = state_root
        host = spec.gateway.host
        url_host = f"[{host}]" if host == "::1" else host
        values["CHATCOPILOT_GATEWAY_URL"] = f"ws://{url_host}:{gateway_port}"
    else:
        cc_connect_bin = str(values.get("CHATCOPILOT_CC_CONNECT_BIN", "") or "").strip()
        if not cc_connect_bin:
            cc_connect_bin = private_cc_connect_bin(runtime_root, home=home)
        if (
            any(character in cc_connect_bin for character in ("\r", "\n", "\x00"))
            or not Path(cc_connect_bin).is_absolute()
        ):
            raise ValueError("cc_connect_bin_invalid")
        values.update(
            {
                "CHATCOPILOT_CC_CONNECT_BIN": cc_connect_bin,
                "CHATCOPILOT_CC_HOME": legacy_configuration_home(cc_config_dir, home=home),
                "CHATCOPILOT_CC_CONNECT_CONFIG_DIR": cc_config_dir,
                "CHATCOPILOT_CC_PROJECT_NAME": (
                    deploy.project_name or f"chatcopilot-{instance_id}"
                ),
            }
        )
    return values


def private_cc_connect_bin(runtime_root_value: str, *, home: Path) -> str:
    if runtime_root_value:
        runtime_root_value = expand_leading_home(runtime_root_value, home=home)
        if any(character in runtime_root_value for character in ("\r", "\n", "\x00")):
            raise ValueError("agentstrata_runtime_root_invalid")
        runtime_root = Path(runtime_root_value)
        if not runtime_root.is_absolute():
            raise ValueError("agentstrata_runtime_root_invalid")
    else:
        runtime_root = home / ".local" / "share" / "agentstrata"
    return str(
        runtime_root
        / "node-tools"
        / f"cc-connect-{_CC_CONNECT_VERSION}"
        / "node_modules"
        / ".bin"
        / "cc-connect"
    )


def _tool_pack_runtime_defaults(tool_packs: Iterable[str]) -> dict[str, str]:
    """Derive runtime env from explicitly selected tool packs."""
    from chatcopilot.botspec.registry import get_tool_pack_entry

    http_modules: list[str] = []
    for tool_pack in tool_packs:
        entry = get_tool_pack_entry(tool_pack)
        if entry is None:
            continue
        for module in entry.http_route_modules:
            if module not in http_modules:
                http_modules.append(module)

    values: dict[str, str] = {}
    if http_modules:
        values["CHATCOPILOT_HTTP_ROUTE_MODULES"] = ",".join(http_modules)
    return values



def runtime_environment_keys(spec) -> tuple[str, ...]:
    return tuple(dict.fromkeys((
        "CHATCOPILOT_INSTANCE_ID",
        "CHATCOPILOT_HOME",
        "CHATCOPILOT_BOT_SPEC",
        "CHATCOPILOT_SOURCE_BOT_SPEC",
        "CHATCOPILOT_ENV_FILE",
        "CHATCOPILOT_WORKSPACE_ROOT",
        "WORKSPACE_ROOT",
        "CHATCOPILOT_LOG_DIR",
        "CHATCOPILOT_CC_HOME",
        "CHATCOPILOT_CC_CONNECT_CONFIG_DIR",
        "CHATCOPILOT_CC_PROJECT_NAME",
        "CHATCOPILOT_DISPLAY_NAME",
        "CHATCOPILOT_GATEWAY_PORT",
        "CHATCOPILOT_GATEWAY_TOKEN",
        "CHATCOPILOT_GATEWAY_STATE_ROOT",
        "CHATCOPILOT_GATEWAY_URL",
        "CHATCOPILOT_CC_CONNECT_BIN",
        "CHATCOPILOT_HTTP_ROUTE_MODULES",
        "CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT",
        "CHATCOPILOT_CODEBASE_CACHE_ROOT",
        "CHATCOPILOT_GIT_AUTHOR_NAME",
        "CHATCOPILOT_GIT_AUTHOR_EMAIL",
        f"{spec.llm.env_prefix}_API_KEY",
        f"{spec.llm.env_prefix}_BASE_URL",
        f"{spec.llm.env_prefix}_MODEL",
        f"{spec.llm.env_prefix}_TIMEOUT",
        f"{spec.llm.env_prefix}_ROUTER_ENABLED",
        f"{spec.llm.env_prefix}_ROUTER_MODE",
        f"{spec.llm.env_prefix}_ROUTER_CODE_PREFIXES",
        f"{spec.llm.env_prefix}_ROUTER_CHAT_PREFIXES",
        f"{spec.llm.env_prefix}_CODE_PROVIDER",
        f"{spec.llm.env_prefix}_CODE_MODEL",
        f"{spec.llm.env_prefix}_CODE_REASONING_EFFORT",
        f"{spec.llm.env_prefix}_CODE_PROFILES_JSON",
        f"{spec.llm.env_prefix}_CODE_TASK_PROFILE",
        f"{spec.llm.env_prefix}_CODE_COMMAND",
        f"{spec.llm.env_prefix}_CODE_WORKDIR_ENV",
        f"{spec.llm.env_prefix}_CODE_TIMEOUT_SECONDS",
        "CHATCOPILOT_ADD_OWNER_IDS",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "QQ_ACCOUNT",
        "CHATCOPILOT_QQ_ONEBOT_WS_URL",
        "QQ_ACCESS_TOKEN",
        "QQ_ALLOW_FROM",
        "QQ_WEBUI_PORT",
        "TAVILY_API_KEY",
        "GITHUB_MCP_AUTHORIZATION",
        *mcp_env_ref_keys(spec),
    )))


def mcp_env_ref_keys(spec) -> tuple[str, ...]:
    if not spec.tools.mcp.servers:
        return ()
    path = spec.resolve_path(spec.tools.mcp.servers)
    if path is None or not path.is_file():
        return ()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ()
    servers = data.get("servers", []) if isinstance(data, dict) else []
    if not isinstance(servers, list):
        return ()
    keys: list[str] = []
    seen: set[str] = set()
    for item in servers:
        if not isinstance(item, dict):
            continue
        item = resolve_catalog_server(item) or item
        for field in ("env", "headers"):
            mapping = item.get(field, {})
            if not isinstance(mapping, dict):
                continue
            for value in mapping.values():
                match = re.match(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$", str(value).strip())
                if not match:
                    continue
                key = match.group(1)
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
    return tuple(keys)



def exported_environment(values: Mapping[str, str], ordered_keys: Iterable[str]) -> dict[str, str]:
    selected = set(ordered_keys)
    return {key: value for key, value in values.items() if value and (
        key in selected or key.startswith(("CHATCOPILOT_", "FEISHU_", "QQ_")) or key == "WORKSPACE_ROOT")}


def legacy_configuration_home(config_dir: str, *, home: Path) -> str:
    normalized = config_dir.replace("\\", "/")
    suffix = "/.cc-connect"
    if normalized.endswith(suffix):
        return normalized[:-len(suffix)] or str(home)
    return str(Path(config_dir).expanduser().parent) if config_dir else ""
