"""Instance-bound Console reads of configuration and persistent observations."""
from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from chatcopilot.botspec.inspection import declared_configuration
from chatcopilot.botspec.provisioning import read_private_env_file
from chatcopilot.core.observability_redaction import (
    collect_observability_secrets, default_observability_roots, redact_observability_payload,
)
from chatcopilot.gateway.state_store import GatewayStateError
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.gateway import observation_queries
from chatcopilot.gateway.observation_queries import RunFilter
from chatcopilot.gateway.read_model import gateway_run, gateway_runs
from console.control.instances import BotInstance
from console.control.yaml_io import load_yaml_mapping_or_empty


def _context(inst: BotInstance) -> tuple[Path | None, dict[str, str], tuple[str, ...]]:
    spec_path = Path(inst.bot_spec)
    config = load_yaml_mapping_or_empty(spec_path)
    gateway = config.get("gateway") or {}
    env_path = Path(inst.env_file) if inst.env_file else spec_path.parent / "local.env"
    try:
        values = read_private_env_file(env_path, allowed_parent=env_path.parent) if env_path.exists() else {}
    except ValueError as exc:
        raise GatewayStateError("Instance environment is unavailable or unsafe") from exc
    key = str(gateway.get("state_root_env") or "CHATCOPILOT_GATEWAY_STATE_ROOT")
    value = values.get(key, "")
    if value and not Path(value).is_absolute():
        raise ValueError("Gateway state root must be absolute")
    secrets = tuple(set(collect_observability_secrets(values)) | set(collect_observability_secrets()))
    return Path(value) if value else None, values, secrets


def _safe(inst: BotInstance, payload: Any, root: Path | None, secrets: tuple[str, ...]) -> Any:
    if payload is None:
        return None
    result = redact_observability_payload(payload, secrets=secrets,
                                          roots=default_observability_roots(root or Path(inst.bot_spec).parent))
    safe = result.value
    if result.truncated and safe.get("state") == "available":
        safe["state"] = "truncated"
    return {**safe, "instance_id": inst.instance_id, "sanitization_truncated": result.truncated}


def reader(inst: BotInstance) -> tuple[ObservationStore, Path, tuple[str, ...]]:
    root, _, secrets = _context(inst)
    if root is None:
        raise ValueError("Gateway state root is not configured")
    return ObservationStore(root, secrets=secrets), root, secrets


def snapshot(inst: BotInstance, run_id: str | None = None, filters: RunFilter | None = None) -> dict[str, Any] | None:
    if inst.runtime_kind != "gateway":
        raise ValueError("Instance does not use Gateway")
    store, root, secrets = reader(inst)
    if store.database.exists():
        result = observation_queries.detail(store, run_id) if run_id else observation_queries.history(store, filters or RunFilter())
    else:
        result = gateway_run(root, run_id, secrets=secrets) if run_id else gateway_runs(root)
        if result is not None:
            result.update(source="gateway_state", legacy=True, history_available=False)
    return _safe(inst, result, root, secrets)


def event_page(inst: BotInstance, run_id: str, after: int, limit: int) -> dict[str, Any] | None:
    store, root, secrets = reader(inst)
    if observation_queries.detail(store, run_id) is None:
        return None
    return _safe(inst, observation_queries.events(store, run_id, after=after, limit=limit), root, secrets)


def body(inst: BotInstance, run_id: str, body_id: str) -> dict[str, Any] | None:
    store, root, secrets = reader(inst)
    return _safe(inst, store.body(run_id, body_id), root, secrets)


def metrics(inst: BotInstance, filters: RunFilter) -> dict[str, Any]:
    store, root, secrets = reader(inst)
    return _safe(inst, observation_queries.metrics(store, filters), root, secrets)


def inspection(inst: BotInstance, *, run_id: str | None = None, event_seq: int | None = None) -> dict[str, Any]:
    if event_seq is not None and (not run_id or event_seq < 1):
        raise ValueError("A task and positive event sequence are required")
    root, values, secrets = _context(inst)
    errors = []
    try:
        current = declared_configuration(Path(inst.bot_spec), {**os.environ, **values})
    except (ValueError, OSError, RuntimeError) as exc:
        current = None
        errors.append({"source": "current", "code": type(exc).__name__, "message": "实例配置读取或校验失败"})
    loaded = execution = None
    loaded_meta = None
    if root is not None:
        store = ObservationStore(root, secrets=secrets)
        if store.database.exists():
            loaded_meta = store.meta("loaded_configuration")
            if loaded_meta:
                loaded = store.configuration(loaded_meta["config_id"])
            if run_id:
                record = observation_queries.detail(store, run_id)
                if record and record["run"].get("config_id"):
                    key = record["run"]["config_id"]
                    if event_seq is not None:
                        page = observation_queries.events(store, run_id, after=event_seq - 1, limit=1)
                        event = next((event for event in page["observations"] if event["seq"] == event_seq), None)
                        if event is None:
                            raise ValueError("Event does not belong to selected task")
                        key = event["data"].get("configuration_id")
                    execution = store.configuration(key) if key else None
    return _safe(inst, {"current": current, "loaded": loaded, "execution": execution,
        "loaded_meta": loaded_meta, "run_id": run_id, "errors": errors, "generated_at": time.time(),
        "sources": {"current": "botspec", "loaded": "runtime_projection", "execution": "task_record"},
        "pending_changes": bool(current and loaded and current.get("configuration_revision") != loaded.get("source_revision", loaded.get("configuration_revision"))),
        "loaded_stale": not loaded_meta or time.time() - loaded_meta["observed_at"] > 15,
    }, root, secrets)
