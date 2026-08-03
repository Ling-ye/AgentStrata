"""BotSpec RAG source configuration helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from chatcopilot.botspec.model import BotSpec, ValidationIssue
from chatcopilot.contracts.runtime import RagSourceConfig

_ENV_REF_RE = re.compile(r"(\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%)")
_DEFAULT_INCLUDE = ("*.md", "*.txt", "*.yaml", "*.yml", "*.json")
_DEFAULT_MAX_CHUNK_CHARS = 1200



def load_rag_source_configs(spec: BotSpec) -> tuple[RagSourceConfig, ...]:
    """Load resolved RAG source configs for runtime use."""

    path = spec.resolve_path(spec.context.rag.sources)
    if path is None or not path.is_file():
        return ()
    data = _load_yaml(path)
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        return ()

    out: list[RagSourceConfig] = []
    for idx, item in enumerate(raw_sources):
        raw = item if isinstance(item, dict) else {"path": item}
        cfg = _parse_source(raw, spec=spec, field=f"rag.sources[{idx}]", validate=False)
        if cfg is not None:
            out.append(cfg)
    return tuple(out)


def validate_rag_sources(spec: BotSpec) -> list[ValidationIssue]:
    """Validate optional RAG source declarations."""

    path = spec.resolve_path(spec.context.rag.sources)
    if path is None or not path.is_file():
        return []
    try:
        data = _load_yaml(path)
    except Exception as exc:  # noqa: BLE001
        return [ValidationIssue("error", f"context.rag.sources YAML 解析失败: {exc}", "context.rag.sources")]

    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list):
        return [ValidationIssue("error", "context.rag.sources 顶层必须包含 sources 列表", "context.rag.sources")]

    issues: list[ValidationIssue] = []
    for idx, item in enumerate(raw_sources):
        field = f"rag.sources[{idx}]"
        if not isinstance(item, (dict, str)):
            issues.append(ValidationIssue("error", "每个 RAG source 必须是 mapping 或 path 字符串", field))
            continue
        raw = item if isinstance(item, dict) else {"path": item}
        try:
            _parse_source(raw, spec=spec, field=field, validate=True)
        except ValueError as exc:
            issues.append(ValidationIssue("error", str(exc), field))
    return issues


def _parse_source(raw: dict[str, Any], *, spec: BotSpec, field: str, validate: bool) -> RagSourceConfig | None:
    raw_path = str(raw.get("path", "")).strip()
    if not raw_path:
        if validate:
            raise ValueError("RAG source path 不能为空")
        return None

    path = _resolve_source_path(raw_path, spec=spec, validate=validate)
    if path is None:
        return None

    include = tuple(_str_list(raw.get("include", _DEFAULT_INCLUDE))) or _DEFAULT_INCLUDE
    exclude = tuple(_str_list(raw.get("exclude", ())))
    max_chunk_chars = _positive_int(raw.get("max_chunk_chars"), default=_DEFAULT_MAX_CHUNK_CHARS)
    if max_chunk_chars < 200:
        if validate:
            raise ValueError("max_chunk_chars 必须大于等于 200")
        max_chunk_chars = 200

    label = str(raw.get("label", "")).strip() or raw_path
    return RagSourceConfig(
        path=path,
        label=label,
        include=include,
        exclude=exclude,
        max_chunk_chars=max_chunk_chars,
    )


def _resolve_source_path(raw_path: str, *, spec: BotSpec, validate: bool) -> Path | None:
    uses_env = bool(_ENV_REF_RE.search(raw_path))
    expanded = os.path.expandvars(raw_path)
    if uses_env and _ENV_REF_RE.search(expanded):
        if validate:
            raise ValueError(f"RAG source path 引用了未设置的环境变量: {raw_path}")
        return None

    candidate = Path(expanded).expanduser()
    bot_dir = spec.source_path.parent.resolve()
    if candidate.is_absolute():
        if not uses_env:
            raise ValueError("RAG source path 不能写明文绝对路径；请使用相对路径或环境变量引用")
        return candidate.resolve()

    resolved = (bot_dir / candidate).resolve()
    try:
        resolved.relative_to(bot_dir)
    except ValueError as exc:
        raise ValueError("RAG source path 不能逃逸 BotSpec 目录；请使用 bot 目录内相对路径或环境变量引用") from exc
    return resolved


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


__all__ = ["RagSourceConfig", "load_rag_source_configs", "validate_rag_sources"]
