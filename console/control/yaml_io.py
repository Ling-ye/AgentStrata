"""Fail-soft YAML readers shared by read-only console projections."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def load_yaml_or_empty(path: Path) -> Any:
    """Load UTF-8 YAML, returning an empty mapping for missing/invalid input."""
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_yaml_mapping_or_empty(path: Path) -> dict[str, Any]:
    value = load_yaml_or_empty(path)
    return value if isinstance(value, dict) else {}


__all__ = ["load_yaml_mapping_or_empty", "load_yaml_or_empty"]
