"""Instance-bound Console reads of configuration and persistent observations."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from chatcopilot.botspec.inspection import expected_configuration
from chatcopilot.botspec.provisioning import read_private_env_file
from chatcopilot.core.observability_redaction import (
    bound_observability_payload,
)
from chatcopilot.gateway.state_store import GatewayStateError
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.gateway import observation_queries
from chatcopilot.gateway.observation_queries import RunFilter
from console.control.instances import BotInstance
from console.control.yaml_io import load_yaml_mapping_or_empty


def _context(inst: BotInstance) -> tuple[Path | None, dict[str, str]]:
    spec_path = Path(inst.bot_spec)
    config = load_yaml_mapping_or_empty(spec_path)
    gateway = config.get("gateway") or {}
    env_path = Path(inst.env_file) if inst.env_file else spec_path.parent / "local.env"
    try:
        values = read_private_env_file(env_path, allowed_parent=env_path.parent) if env_path.exists() or env_path.is_symlink() else {}
    except ValueError as exc:
        raise GatewayStateError("Instance environment is unavailable or unsafe") from exc
    key = str(gateway.get("state_root_env") or "CHATCOPILOT_GATEWAY_STATE_ROOT")
    value = values.get(key, "")
    if value and not Path(value).is_absolute():
        raise ValueError("Gateway state root must be absolute")
    return Path(value) if value else None, values


def _safe(inst: BotInstance, payload: Any) -> Any:
    if payload is None:
        return None
    result = bound_observability_payload(payload)
    safe = result.value
    if result.truncated and safe.get("state") == "available":
        safe["state"] = "truncated"
    return {**safe, "instance_id": inst.instance_id, "sanitization_truncated": result.truncated}


def reader(inst: BotInstance) -> ObservationStore:
    root, _ = _context(inst)
    if root is None:
        raise ValueError("Gateway state root is not configured")
    return ObservationStore(root)


def snapshot(inst: BotInstance, run_id: str | None = None, filters: RunFilter | None = None) -> dict[str, Any] | None:
    if inst.runtime_kind != "gateway":
        raise ValueError("Instance does not use Gateway")
    store = reader(inst)
    result = observation_queries.detail(store, run_id) if run_id else observation_queries.history(store, filters or RunFilter())
    return _safe(inst, result)


def event_page(inst: BotInstance, run_id: str, after: int, limit: int) -> dict[str, Any] | None:
    store = reader(inst)
    if observation_queries.detail(store, run_id) is None:
        return None
    return _safe(inst, observation_queries.events(store, run_id, after=after, limit=limit))


def body(inst: BotInstance, run_id: str, body_id: str) -> dict[str, Any] | None:
    store = reader(inst)
    return _safe(inst, store.body(run_id, body_id))


def trace_record(inst: BotInstance, run_id: str, *, span_id: str = "", after: int = 0) -> dict[str, Any]:
    from chatcopilot.core.trace_archive import TraceArchive
    store = reader(inst)
    if observation_queries.detail(store, run_id) is None:
        raise ValueError("Run not found")
    reference = store.meta("trace:" + run_id) or {"capture_state": "not_recorded"}
    if not reference.get("trace_ref"):
        return {**reference, "spans": []}
    archive = TraceArchive(store.root / "traces")
    kwargs = {"source": {"kind": "robot_task", "run_id": run_id}, "sha256": reference["sha256"]}
    if span_id:
        return archive.step(reference["trace_ref"], span_id, **kwargs)
    return archive.summary(reference["trace_ref"], after=after, **kwargs)


def metrics(inst: BotInstance, filters: RunFilter) -> dict[str, Any]:
    store = reader(inst)
    return _safe(inst, observation_queries.metrics(store, filters))


def inspection(inst: BotInstance, *, run_id: str | None = None, event_seq: int | None = None) -> dict[str, Any]:
    if event_seq is not None and (not run_id or event_seq < 1):
        raise ValueError("A task and positive event sequence are required")
    root, values = _context(inst)
    errors = []
    try:
        local_env = Path(inst.bot_spec).parent / "local.env"
        saved = read_private_env_file(local_env, allowed_parent=local_env.parent) if local_env.exists() or local_env.is_symlink() else values
        current = expected_configuration(Path(inst.bot_spec), saved, home=Path.home())
    except (ValueError, OSError, RuntimeError) as exc:
        current = None
        errors.append({"source": "current", "code": type(exc).__name__, "message": "实例配置读取或校验失败"})
    loaded = execution = None
    loaded_meta = None
    runtime_stopped = False
    try:
        if root is not None:
            store = ObservationStore(root)
            if store.database.exists():
                loaded_meta = store.meta("loaded_configuration")
                runtime_status = store.meta("runtime_status") or {}
                runtime_stopped = bool(loaded_meta and runtime_status.get("state") == "stopped"
                    and runtime_status.get("generation") == loaded_meta.get("generation")
                    and runtime_status.get("observed_at", 0) >= loaded_meta["observed_at"])
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
    except (OSError, GatewayStateError) as exc:
        loaded = None
        errors.append({"source": "loaded", "code": type(exc).__name__, "message": "运行快照读取失败"})
    stale = runtime_stopped or not loaded_meta or not 0 <= time.time() - loaded_meta["observed_at"] <= 15
    comparison, reason = configuration_comparison(current, loaded, stale=stale)
    return _safe(inst, {"current": current, "loaded": loaded, "execution": execution,
        "loaded_meta": loaded_meta, "run_id": run_id, "errors": errors, "generated_at": time.time(),
        "sources": {"current": "botspec", "loaded": "runtime_projection", "execution": "task_record"},
        "pending_changes": comparison == "pending",
        "configuration_status": comparison, "configuration_status_reason": reason,
        "loaded_stale": stale,
    })


def configuration_comparison(current, loaded, *, stale: bool) -> tuple[str, str]:
    if not current or not loaded or stale:
        return "unknown", "运行快照缺失、过期或读取失败"
    source = loaded.get("source_revision", loaded.get("configuration_revision"))
    if not source or not loaded.get("environment_revision"):
        return "unknown", "运行快照没有可比较的配置版本"
    if source != current.get("configuration_revision"):
        return "pending", "BotSpec 或引用文件存在尚未加载的变更"
    if loaded.get("environment_revision_version") != current.get("environment_revision_version"):
        return "unknown", "运行快照的环境解析版本不可比较"
    if loaded["environment_revision"] != current.get("effective_environment_revision"):
        return "pending", "有效环境配置存在尚未加载的变更"
    if loaded.get("reference_revision") is None:
        return "unknown", "运行快照未记录完整的引用文件版本"
    if loaded["reference_revision"] != current.get("reference_revision"):
        return "pending", "上下文配置引用存在尚未加载的变更"
    return "applied", "当前配置与服务上报配置一致"
