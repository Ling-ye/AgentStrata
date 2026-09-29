"""Account-scoped discovery results and validation; no provider implementation."""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Callable

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.model_settings import ModelSettingsError


def normalize_models(rows: list[dict], *, codex: bool = False) -> list[dict]:
    result = []
    seen = set()
    for row in rows:
        model = row.get("model", row.get("id")) if codex else row.get("id")
        if not isinstance(model, str) or not model or model in seen:
            continue
        seen.add(model)
        raw = row.get("supportedReasoningEfforts" if codex else "supported_reasoning_efforts")
        efforts = None
        if isinstance(raw, list):
            efforts = []
            for item in raw:
                effort = item.get("reasoningEffort") if isinstance(item, dict) else item
                if isinstance(effort, str) and effort and effort not in efforts:
                    efforts.append(effort)
        context = row.get("contextWindow" if codex else "context_window")
        if type(context) is not int or context <= 0:
            context = None
        name = row.get("displayName") or row.get("name")
        result.append({"id": model, "name": name if isinstance(name, str) and name else model,
            "reasoning_efforts": efforts,
            "default_reasoning_effort": row.get("defaultReasoningEffort") if codex else row.get("default_reasoning_effort"),
            "context_window": context, "hidden": row.get("hidden") is True})
    return result


class ModelCatalog:
    def __init__(self, discover: Callable[[dict], list[dict]], identity: Callable[[dict], str]):
        self._discover, self._identity = discover, identity
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _key(self, connection: dict) -> str:
        return digest({"connection": connection, "identity": self._identity(connection)})

    def get(self, connection: dict) -> dict:
        key = self._key(connection)
        with self._lock:
            return copy.deepcopy(self._entries.get(key, {"models": [], "fetched_at": None, "error": None}))

    def refresh(self, connection: dict) -> dict:
        # Discovery is explicit. Last successful results are never cleared by a failed refresh.
        key = self._key(connection)
        try:
            models = normalize_models(self._discover(connection), codex=connection["kind"] == "codex")
            entry = {"models": models, "fetched_at": datetime.now(timezone.utc).isoformat(), "error": None}
        except Exception:
            with self._lock:
                entry = copy.deepcopy(self._entries.get(key, {"models": [], "fetched_at": None}))
            entry["error"] = "模型目录查询失败，请检查连接、凭据及网络"
        with self._lock:
            self._entries[key] = entry
        return copy.deepcopy(entry)

    def validate_save(self, document: dict, previous: dict) -> None:
        for name, profile in document["profiles"].items():
            connection = document["connections"][profile["connection"]]
            before = previous["profiles"].get(name)
            old_connection = previous["connections"].get(profile["connection"])
            if before == profile and connection == old_connection:
                continue
            catalog = self.get(connection)
            if not catalog["fetched_at"] or catalog["error"]:
                raise ModelSettingsError(f"{name}: 请先成功刷新此连接的模型目录")
            model = next((m for m in catalog["models"] if m["id"] == profile["model"]), None)
            if model is None:
                raise ModelSettingsError(f"{name}: 模型未出现在此连接的目录中")
            effort = profile.get("reasoning_effort")
            if effort is not None and effort not in (model["reasoning_efforts"] or []):
                raise ModelSettingsError(f"{name}: 接口未声明此推理强度")
