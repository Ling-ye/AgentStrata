"""Fail-forward state transition for instance-level main-agent changes."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BackendTransition:
    previous_backend: str
    target_backend: str
    state_deleted: bool
    audit_path: Path


def prepare_backend_deployment(
    *,
    instance_id: str,
    target_backend: str,
    workspace_root: str | Path,
) -> BackendTransition:
    """Delete old backend histories before the target deployment begins."""

    root = Path(workspace_root).expanduser().resolve()
    if not instance_id.strip() or target_backend not in {"native", "langgraph", "codex"}:
        raise ValueError("valid instance_id and target_backend are required")
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError(f"refusing broad backend-state root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    control = root / ".agent-backend.json"
    audit = root / ".agent-backend-audit.jsonl"
    previous = ""
    if control.is_file():
        payload = json.loads(control.read_text(encoding="utf-8"))
        if str(payload.get("instance_id") or "") == instance_id:
            previous = str(payload.get("backend") or "")

    deleted = bool(previous and previous != target_backend)
    if deleted:
        _delete_backend_histories(root)
        _append_audit(
            audit,
            instance_id=instance_id,
            event="state_deleted",
            previous_backend=previous,
            target_backend=target_backend,
        )
    if previous != target_backend:
        _write_json_atomic(
            control,
            {
                "schema_version": 1,
                "instance_id": instance_id,
                "backend": target_backend,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    _append_audit(
        audit,
        instance_id=instance_id,
        event="target_deploy_started",
        previous_backend=previous,
        target_backend=target_backend,
    )
    return BackendTransition(previous, target_backend, deleted, audit)


def _delete_backend_histories(root: Path) -> None:
    targets: list[Path] = []
    for pattern in ("transcripts", ".backend-sessions"):
        targets.extend(path for path in root.rglob(pattern) if path.is_dir())
    for target in sorted(set(targets), key=lambda path: len(path.parts), reverse=True):
        resolved = target.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"backend state target escaped workspace: {resolved}") from exc
        shutil.rmtree(resolved)


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


__all__ = ["BackendTransition", "prepare_backend_deployment"]
