"""Small in-process cache for subagent results."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Sequence

from chatcopilot.agent.subagents.spec import CachePolicySpec
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.contracts.tools import ToolDef


@dataclass(frozen=True)
class CacheEntry:
    value: str
    outputs: tuple[str, ...]
    expires_at: float


class SubagentResultCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at and entry.expires_at < time.time():
            self._entries.pop(key, None)
            return None
        return entry

    def set(self, key: str, *, value: str, outputs: Sequence[str], ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else 0
        self._entries[key] = CacheEntry(value=value, outputs=tuple(outputs), expires_at=expires_at)


GLOBAL_SUBAGENT_CACHE = SubagentResultCache()


def build_cache_key(
    *,
    subagent_name: str,
    version: str,
    model: str,
    prompt_fingerprint: str,
    tools: Sequence[ToolDef],
    task: TaskPack,
    policy: CachePolicySpec,
) -> str:
    payload = {
        "subagent_name": subagent_name,
        "version": version,
        "model": model,
        "prompt_fingerprint": prompt_fingerprint,
        "toolset_fingerprint": toolset_fingerprint(tools),
        "task": _normalized_task(task, include_resource_hashes=policy.include_resource_hashes),
        "namespace": policy.namespace,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def toolset_fingerprint(tools: Sequence[ToolDef]) -> str:
    values = [
        {
            "name": tool.name,
            "category": tool.category,
            "owner": tool.owner,
            "module": tool.module,
            "risk": str(tool.metadata.get("mcp_risk", "")),
        }
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalized_task(task: TaskPack, *, include_resource_hashes: bool) -> dict[str, object]:
    data = task.to_dict()
    if not include_resource_hashes:
        data = dict(data)
        data.pop("resources", None)
        data.pop("inputs", None)
    else:
        data = dict(data)
        data["resources"] = tuple(_content_hash(r) for r in (data.get("resources") or ()))
        data["inputs"] = tuple(_content_hash(i) for i in (data.get("inputs") or ()))
    return data


def _content_hash(value: Any) -> str:
    """Compute a short content-addressed hash for a resource reference.

    If ``value`` looks like a filesystem path that exists, hash the file
    contents. Otherwise hash the string representation. Missing files produce
    a deterministic ``"missing:<path_hash>"`` placeholder so the cache key
    stays stable without raising.
    """
    text = str(value).strip()
    if not text:
        return ""
    if os.path.isfile(text):
        try:
            h = hashlib.sha256()
            with open(text, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]
        except OSError:
            return f"missing:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "GLOBAL_SUBAGENT_CACHE",
    "SubagentResultCache",
    "build_cache_key",
    "toolset_fingerprint",
]
