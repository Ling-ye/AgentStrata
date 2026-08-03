"""Lightweight ``${VAR}`` / ``${VAR:-default}`` template expander for tool configs.

This helper is shared across ``external_tools`` packages that load YAML config
files and want to support environment variable overrides without pulling in any
extra dependency. It deliberately stays standard-library only and does not
import anything from ``chatcopilot.middleware`` so any tool package can use it
without creating a runtime dependency on the agent runtime.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

_TEMPLATE_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def expand_env_template(value: str, *, environ: Mapping[str, str] | None = None) -> str:
    """Replace ``${VAR}`` and ``${VAR:-default}`` occurrences inside ``value``.

    Unknown variables without a default expand to an empty string, mirroring
    shell ``${VAR}`` behavior. Use ``${VAR:-fallback}`` to provide explicit
    fallbacks.
    """
    env: Mapping[str, str] = environ if environ is not None else os.environ

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        if name in env and env[name] != "":
            return env[name]
        if default is not None:
            return default
        return ""

    return _TEMPLATE_RE.sub(_replace, value)


def expand_in_tree(node: Any, *, environ: Mapping[str, str] | None = None) -> Any:
    """Recursively walk a YAML-decoded structure and expand env templates in strings."""
    if isinstance(node, str):
        return expand_env_template(node, environ=environ)
    if isinstance(node, list):
        return [expand_in_tree(item, environ=environ) for item in node]
    if isinstance(node, dict):
        return {key: expand_in_tree(val, environ=environ) for key, val in node.items()}
    return node


__all__ = ["expand_env_template", "expand_in_tree"]
