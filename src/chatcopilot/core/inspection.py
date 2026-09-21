"""Stable serialization and fingerprints for runtime and operator projections."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: plain(getattr(value, item.name)) for item in fields(value)
                if item.name not in {"source_path", "handler", "role_prompt", "raw"}
                and not item.metadata.get("secret") and not item.metadata.get("private")}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((plain(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()[:24]


def public_configuration(value: Any, *, secrets=()) -> Any:
    """Export non-secret configuration while preserving environment references."""
    from chatcopilot.core.observability_redaction import redact_observability_payload
    def convert(item, key=""):
        if isinstance(item, dict):
            return {name: convert(child, name) for name, child in item.items()}
        if isinstance(item, list):
            return [convert(child, key) for child in item]
        if item is None or item == "":
            return item
        if key.endswith(("_env", "env_prefix")) and isinstance(item, str):
            return item
        return redact_observability_payload({key: item}, secrets=secrets).value[key]
    return convert(plain(value))
