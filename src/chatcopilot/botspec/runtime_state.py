"""Fail-forward state transition for instance-level main-agent changes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chatcopilot.contracts.runtime_adapter import RUNTIME_IDS


@dataclass(frozen=True)
class RuntimeTransition:
    previous_runtime_id: str
    target_runtime_id: str
    state_deleted: bool
    audit_path: Path


def prepare_runtime_deployment(
    *,
    instance_id: str,
    target_runtime_id: str,
    workspace_root: str | Path,
) -> RuntimeTransition:
    """Validate a migration marker before deployment; never delete conversation history."""

    root = Path(workspace_root).expanduser().resolve()
    if not instance_id.strip() or target_runtime_id not in RUNTIME_IDS:
        raise ValueError("valid instance_id and target_runtime_id are required")
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError(f"refusing broad runtime-state root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    control = root / ".agent-runtime.json"
    audit = root / ".agent-runtime-audit.jsonl"
    previous = ""
    if control.is_file():
        payload = json.loads(control.read_text(encoding="utf-8"))
        if str(payload.get("instance_id") or "") == instance_id:
            previous = str(payload.get("runtime_id") or "")

    deleted = bool(previous and previous != target_runtime_id)
    if deleted:
        raise ValueError("Runtime change requires explicit stopped-instance runtime-migrate apply; histories were preserved")
    if previous != target_runtime_id:
        _write_json_atomic(
            control,
            {
                "schema_version": 2,
                "instance_id": instance_id,
                "runtime_id": target_runtime_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    _append_audit(
        audit,
        instance_id=instance_id,
        event="target_deploy_started",
        previous_runtime_id=previous,
        target_runtime_id=target_runtime_id,
    )
    return RuntimeTransition(previous, target_runtime_id, deleted, audit)


def _append_audit(path: Path, **payload: str) -> None:
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)


__all__ = ["RuntimeTransition", "prepare_runtime_deployment"]
