"""Read operator-reviewed native extension tables without allowing policy overrides."""

from __future__ import annotations

from pathlib import Path
import re
from chatcopilot.contracts.model_runtime import digest


def extension_digest(text):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    return digest(tomllib.loads(text))


def validate_extension_ownership(text, host_server_ids):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    native_ids = {
        name
        for name, value in tomllib.loads(text).get("mcp_servers", {}).items()
        if value.get("enabled", True)
    }
    if native_ids.intersection(host_server_ids):
        raise ValueError("one MCP service cannot be enabled in both the host and Codex extensions")


def read_extensions(path: Path) -> str:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    if (
        path.is_symlink()
        or path.resolve() != path
        or not path.is_file()
        or path.stat().st_size > 64 * 1024
    ):
        raise ValueError("Codex extension configuration must be a bounded direct file")
    text = path.read_text(encoding="utf-8")
    value = tomllib.loads(text)
    if set(value) - {"mcp_servers", "plugins", "skills", "apps"}:
        raise ValueError(
            "native extension configuration cannot override model, auth, policy or instructions"
        )
    for server in value.get("mcp_servers", {}).values():
        if not isinstance(server, dict) or set(server).intersection({"env", "http_headers"}):
            raise ValueError("native MCP secrets require environment references, not inline values")
    if "apps" in value:
        apps = value["apps"]
        if not isinstance(apps, dict) or apps.get("_default", {}).get("enabled") is not False:
            raise ValueError(
                "native apps require apps._default.enabled=false and explicit per-app grants"
            )
        if any(
            not isinstance(settings, dict) or type(settings.get("enabled")) is not bool
            for name, settings in apps.items()
            if name != "_default"
        ):
            raise ValueError("each native app must declare enabled explicitly")
    return text


def managed_extension_config(text):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    if "apps" not in tomllib.loads(text):
        return text + "\n[apps._default]\nenabled = false\n"
    return text


def extension_environment(text: str, environment) -> dict[str, str]:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    references = set()
    for server in tomllib.loads(text).get("mcp_servers", {}).values():
        references.update(server.get("env_vars", []))
        if server.get("bearer_token_env_var"):
            references.add(server["bearer_token_env_var"])
        references.update(server.get("env_http_headers", {}).values())
    if any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)
        for name in references
    ):
        raise ValueError("native MCP requires explicit environment variable references")
    if references.intersection(
        {
            "HOME",
            "PATH",
            "TMPDIR",
            "CODEX_HOME",
            "CODEX_SQLITE_HOME",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "NODE_OPTIONS",
        }
    ):
        raise ValueError("native extension credentials cannot replace process control variables")
    return {name: environment[name] for name in references if name in environment}
