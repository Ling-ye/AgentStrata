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
                if item.name not in {"source_path", "handler", "role_prompt", "raw"}}
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
