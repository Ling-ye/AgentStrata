"""BotSpec validation and runtime resolution for ``context.wiki``."""
from __future__ import annotations

import os
import re
from pathlib import Path

from chatcopilot.botspec.model import BotSpec, ValidationIssue

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROLES = {"user", "admin", "owner"}
_CANONICAL_ROOT_ENV = "CHATCOPILOT_WIKI_ROOT"


def validate_wiki_spec(spec: BotSpec) -> list[ValidationIssue]:
    wiki = spec.context.wiki
    if not wiki.enabled:
        return []
    issues: list[ValidationIssue] = []
    if not _ENV_NAME_RE.match(wiki.root_env):
        issues.append(
            ValidationIssue(
                "error",
                "context.wiki.root_env 必须是环境变量名，不能直接填写路径",
                "context.wiki.root_env",
            )
        )
    if wiki.read_role not in _ROLES:
        issues.append(
            ValidationIssue(
                "error",
                "context.wiki.read_role 仅支持 user/admin/owner",
                "context.wiki.read_role",
            )
        )
    if wiki.max_chunk_chars < 200:
        issues.append(
            ValidationIssue(
                "error",
                "context.wiki.max_chunk_chars 必须大于等于 200",
                "context.wiki.max_chunk_chars",
            )
        )
    return issues


def resolve_wiki_root(spec: BotSpec) -> Path | None:
    wiki = spec.context.wiki
    if not wiki.enabled:
        return None
    raw = os.environ.get(wiki.root_env, "").strip()
    if not raw and wiki.root_env != _CANONICAL_ROOT_ENV:
        raw = os.environ.get(_CANONICAL_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


__all__ = ["resolve_wiki_root", "validate_wiki_spec"]
