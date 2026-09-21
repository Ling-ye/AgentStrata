"""Inventory-bound phase-A cutover for the Runtime-only data contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Mapping
from uuid import uuid4

import yaml

from chatcopilot.botspec.provisioning import read_private_env_file
from chatcopilot.botspec.runtime_cutover import migrate_declaration, migration_model_snapshot
from chatcopilot.contracts.runtime_adapter import RUNTIME_IDS
from chatcopilot.evals.models import RESULT_SCHEMA_VERSION
from chatcopilot.gateway.observation_store import ObservationStore
from chatcopilot.gateway.runtime_cutover import (
    backup_database,
    check_migration_lease,
    migrate_database,
    migration_session,
)


INVENTORY_SCHEMA_VERSION = 1
CUTOVER_RECEIPT_SCHEMA_VERSION = 1
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling", "waiting_approval"})
_UNVERSIONED_EVALUATION_FIELDS = frozenset(
    {"evaluation_id", "status", "targets", "trials", "config_snapshot"}
)


@dataclass(frozen=True)
class CutoverInstance:
    instance_id: str
    bot_spec: Path
    private_env: Path
    gateway_state_root: Path
    workspace_root: Path
    evaluation_root: Path
    expected_runtime_id: str


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _direct_path(value: Any, *, label: str, kind: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError(f"{label} must be an absolute direct path")
    if kind == "file" and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError(f"{label} must be an ordinary file")
    if kind == "directory" and (not path.is_dir() or not stat.S_ISDIR(path.stat().st_mode)):
        raise ValueError(f"{label} must be a directory")
    info = path.stat()
    if os.name == "posix" and info.st_uid != os.getuid():
        raise ValueError(f"{label} must be owned by the current user")
    if kind == "file" and info.st_nlink != 1:
        raise ValueError(f"{label} must have exactly one hard link")
    return path


def _load_inventory(path: Path) -> tuple[str, tuple[CutoverInstance, ...]]:
    inventory = _direct_path(path.absolute(), label="inventory", kind="file")
    raw_bytes = inventory.read_bytes()
    payload = yaml.safe_load(raw_bytes) or {}
    if not isinstance(payload, dict) or payload.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        raise ValueError("runtime cutover inventory schema is unsupported")
    rows = payload.get("instances")
    if not isinstance(rows, list) or not rows:
        raise ValueError("runtime cutover inventory must list instances")
    fields = {"instance_id", "bot_spec", "private_env", "gateway_state_root", "workspace_root", "evaluation_root", "expected_runtime_id"}
    instances: list[CutoverInstance] = []
    identities: set[str] = set()
    paths: set[Path] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(f"instances[{index}] must contain the exact inventory fields")
        instance_id = str(row["instance_id"] or "").strip()
        runtime_id = str(row["expected_runtime_id"] or "").strip().lower()
        if not instance_id or instance_id in identities or runtime_id not in RUNTIME_IDS:
            raise ValueError(f"instances[{index}] has an invalid or duplicate identity")
        identities.add(instance_id)
        values = {
            "bot_spec": _direct_path(row["bot_spec"], label=f"instances[{index}].bot_spec", kind="file"),
            "private_env": _direct_path(row["private_env"], label=f"instances[{index}].private_env", kind="file"),
            "gateway_state_root": _direct_path(row["gateway_state_root"], label=f"instances[{index}].gateway_state_root", kind="directory"),
            "workspace_root": _direct_path(row["workspace_root"], label=f"instances[{index}].workspace_root", kind="directory"),
            "evaluation_root": _direct_path(row["evaluation_root"], label=f"instances[{index}].evaluation_root", kind="directory"),
        }
        for candidate in values.values():
            if candidate in paths:
                raise ValueError(f"instances[{index}] reuses a path from another instance")
            paths.add(candidate)
        if os.name == "posix" and stat.S_IMODE(values["private_env"].stat().st_mode) != 0o600:
            raise ValueError(f"instances[{index}].private_env must have mode 0600")
        instances.append(CutoverInstance(instance_id=instance_id, expected_runtime_id=runtime_id, **values))
    return hashlib.sha256(raw_bytes).hexdigest(), tuple(instances)


def _sqlite_schema(path: Path, table: str) -> int | None:
    if not path.is_file():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as connection:
        row = connection.execute(f"SELECT value FROM {table} WHERE key='schema_version'").fetchone()
    return int(row[0]) if row is not None else None


def _evaluation_schemas(root: Path) -> tuple[int | None, ...]:
    schemas: set[int | None] = set()
    for path in root.glob("*/result.json"):
        if path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError(f"unsafe Evaluation artifact: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Evaluation result is not an object: {path}")
        if "schema_version" not in payload:
            if not _UNVERSIONED_EVALUATION_FIELDS.issubset(payload):
                raise ValueError(f"Unrecognized unversioned Evaluation result: {path}")
            schemas.add(None)
            continue
        version = payload["schema_version"]
        if type(version) is not int:
            raise ValueError(f"Evaluation result has an invalid schema version: {path}")
        schemas.add(version)
    if not schemas.issubset({None, 2, RESULT_SCHEMA_VERSION}):
        raise ValueError("Evaluation store contains an unsupported source schema")
    return tuple(sorted(schemas, key=lambda item: -1 if item is None else item))


def _job_inventory(root: Path) -> tuple[Path, ...]:
    paths = tuple(sorted(root.glob("**/jobs/job_*/status.json")))
    for path in paths:
        if path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError(f"unsafe worker status: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if str(payload.get("status") or "") in _ACTIVE_JOB_STATES:
            raise ValueError(f"unfinished worker prevents runtime cutover: {path.parent.name}")
    return paths


def _instance_plan(instance: CutoverInstance) -> dict[str, Any]:
    before = instance.bot_spec.read_bytes()
    raw = yaml.safe_load(before) or {}
    if not isinstance(raw, dict) or str(raw.get("id") or "") != instance.instance_id:
        raise ValueError(f"BotSpec identity does not match inventory: {instance.instance_id}")
    environment = dict(os.environ)
    environment.update(read_private_env_file(instance.private_env, allowed_parent=instance.private_env.parent))
    migrated = migrate_declaration(raw, environment=environment)
    runtime_id = str((migrated.get("agents") or {}).get("runtime") or "")
    if runtime_id != instance.expected_runtime_id:
        raise ValueError(f"expected runtime does not match BotSpec: {instance.instance_id}")
    models = migration_model_snapshot(migrated, environment)
    check_migration_lease(instance.gateway_state_root)
    gateway = migrate_database(instance.gateway_state_root)
    observation_schema = _sqlite_schema(instance.gateway_state_root / "observability" / "index.sqlite3", "meta")
    if observation_schema not in {None, 1, 2}:
        raise ValueError("Observation store contains an unsupported source schema")
    evaluation_schemas = _evaluation_schemas(instance.evaluation_root)
    jobs = _job_inventory(instance.workspace_root)
    target_device = instance.gateway_state_root.stat().st_dev
    for _, source in _archive_targets(instance):
        _validate_tree(source)
        if source.stat().st_dev != target_device:
            raise ValueError(f"cutover source is on another filesystem: {source}")
    database = instance.gateway_state_root / "gateway.sqlite3"
    if shutil.disk_usage(instance.gateway_state_root).free < (database.stat().st_size * 2 if database.is_file() else 0):
        raise ValueError(f"insufficient cutover backup space: {instance.instance_id}")
    return {
        "instance_id": instance.instance_id,
        "runtime_id": runtime_id,
        "bot_spec_sha256": hashlib.sha256(before).hexdigest(),
        "private_env_sha256": _file_sha256(instance.private_env),
        "model_routes": models,
        "gateway": gateway,
        "observation_schema": observation_schema,
        "evaluation_schemas": list(evaluation_schemas),
        "worker_records": len(jobs),
        "archive_targets": [label for label, _ in _archive_targets(instance)],
        "paths": {key: str(value) for key, value in asdict(instance).items() if isinstance(value, Path)},
    }


def check_inventory(inventory: Path) -> dict[str, Any]:
    inventory_digest, instances = _load_inventory(inventory)
    plan = {"schema_version": 1, "inventory_digest": inventory_digest, "instances": [_instance_plan(item) for item in instances]}
    return {**plan, "plan_digest": _digest(plan), "applied": False}


def _validate_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        if os.name == "posix" and info.st_uid != os.getuid():
            raise ValueError(f"cutover source has the wrong owner: {path}")
        if stat.S_ISLNK(info.st_mode):
            os.readlink(path)
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError(f"cutover source contains a hard-linked file: {path}")


def _archive_targets(instance: CutoverInstance) -> tuple[tuple[str, Path], ...]:
    candidates: list[tuple[str, Path]] = []
    observation = instance.gateway_state_root / "observability"
    if observation.exists():
        candidates.append(("gateway/observability", observation))
    if any(instance.evaluation_root.iterdir()):
        candidates.append(("evaluations", instance.evaluation_root))
    for name in (".agent-backend.json", ".agent-backend-audit.jsonl"):
        path = instance.workspace_root / name
        if path.exists():
            candidates.append(("workspace/" + name, path))
    names = {"transcripts", "jobs", "backend-sessions", ".backend-sessions", "runtime-sessions", ".runtime-sessions"}
    for path in sorted(instance.workspace_root.rglob("*")):
        if path.is_dir() and not path.is_symlink() and path.name in names:
            candidates.append(("workspace/" + path.relative_to(instance.workspace_root).as_posix(), path))
    selected: list[tuple[str, Path]] = []
    for label, path in sorted(candidates, key=lambda item: len(item[1].parts)):
        if any(path == parent or parent in path.parents for _, parent in selected):
            continue
        selected.append((label, path))
    return tuple(selected)


def _archive_manifest(root: Path, sources: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    files = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            encoded = os.fsencode(target)
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "symlink",
                    "size": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "link_target": target,
                }
            )
        elif stat.S_ISREG(info.st_mode):
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "file",
                    "size": info.st_size,
                    "sha256": _file_sha256(path),
                }
            )
    return {"schema_version": 1, "archived_at": datetime.now(timezone.utc).isoformat(), "sources": dict(sources), "record_count": len(files), "files": files}


def _write_private_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _apply_instance(instance: CutoverInstance, expected: Mapping[str, Any]) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update(read_private_env_file(instance.private_env, allowed_parent=instance.private_env.parent))
    original = instance.bot_spec.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected["bot_spec_sha256"]:
        raise ValueError(f"BotSpec changed after cutover check: {instance.instance_id}")
    if _file_sha256(instance.private_env) != expected["private_env_sha256"]:
        raise ValueError(f"private env changed after cutover check: {instance.instance_id}")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = instance.gateway_state_root / f"runtime-cutover-{timestamp}-{uuid4().hex[:8]}"
    targets = _archive_targets(instance)
    for _, path in targets:
        _validate_tree(path)
        if path.stat().st_dev != instance.gateway_state_root.stat().st_dev:
            raise ValueError(f"cutover source is on another filesystem: {path}")
    archive.mkdir(mode=0o700)
    raw = yaml.safe_load(original) or {}
    migrated = migrate_declaration(raw, environment=environment)
    sources: dict[str, dict[str, Any]] = {}
    with migration_session(instance.gateway_state_root) as session:
        gateway_backup = backup_database(instance.gateway_state_root, held_session=session)
        gateway = migrate_database(instance.gateway_state_root, apply=True, held_session=session)
        if migrated != raw:
            backup = archive / "botspec.pre-cutover.yaml"
            backup.write_bytes(original)
            backup.chmod(0o600)
            rendered = yaml.safe_dump(migrated, sort_keys=False, allow_unicode=True).encode()
            temporary = instance.bot_spec.with_name(instance.bot_spec.name + ".cutover-" + uuid4().hex)
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(stat.S_IMODE(instance.bot_spec.stat().st_mode))
            temporary.replace(instance.bot_spec)
        for label, source in targets:
            target = archive / label
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source_schema = expected.get("observation_schema") if label == "gateway/observability" else expected.get("evaluation_schemas") if label == "evaluations" else None
            sources[label] = {"original_path": str(source), "source_schema": source_schema}
            source.rename(target)
        instance.evaluation_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        ObservationStore(instance.gateway_state_root, writable=True)
        _write_private_json(instance.workspace_root / ".agent-runtime.json", {"schema_version": 2, "instance_id": instance.instance_id, "runtime_id": instance.expected_runtime_id, "cutover_at": datetime.now(timezone.utc).isoformat()})
    manifest = _archive_manifest(archive, sources)
    manifest_path = archive / "manifest.json"
    _write_private_json(manifest_path, manifest)
    return {"instance_id": instance.instance_id, "runtime_id": instance.expected_runtime_id, "gateway": gateway, "gateway_backup": gateway_backup, "archive": str(archive), "archive_manifest": str(manifest_path), "archive_manifest_sha256": _file_sha256(manifest_path), "schemas": {"gateway": 3, "observation": 2, "evaluation": RESULT_SCHEMA_VERSION, "runtime_session_binding": 4}}


def apply_inventory(inventory: Path, *, plan_digest: str) -> dict[str, Any]:
    plan = check_inventory(inventory)
    if not plan_digest or plan["plan_digest"] != plan_digest:
        raise ValueError("plan digest does not match the current inventory and source state")
    inventory_digest, instances = _load_inventory(inventory)
    expected = {item["instance_id"]: item for item in plan["instances"]}
    receipt = {"schema_version": CUTOVER_RECEIPT_SCHEMA_VERSION, "inventory_digest": inventory_digest, "plan_digest": plan_digest, "created_at": datetime.now(timezone.utc).isoformat(), "instances": [_apply_instance(item, expected[item.instance_id]) for item in instances]}
    receipt_path = inventory.with_name(inventory.name + ".receipt.json")
    _write_private_json(receipt_path, receipt)
    return {**receipt, "receipt": str(receipt_path), "applied": True}


def verify_inventory(inventory: Path, receipt_path: Path) -> dict[str, Any]:
    inventory_digest, instances = _load_inventory(inventory)
    receipt_file = _direct_path(receipt_path.absolute(), label="receipt", kind="file")
    if os.name == "posix" and stat.S_IMODE(receipt_file.stat().st_mode) != 0o600:
        raise ValueError("cutover receipt must have mode 0600")
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    if receipt.get("schema_version") != CUTOVER_RECEIPT_SCHEMA_VERSION or receipt.get("inventory_digest") != inventory_digest:
        raise ValueError("cutover receipt does not match the inventory")
    by_id = {item["instance_id"]: item for item in receipt.get("instances", [])}
    verified = []
    for instance in instances:
        record = by_id.get(instance.instance_id)
        if not isinstance(record, dict):
            raise ValueError(f"cutover receipt is missing instance: {instance.instance_id}")
        manifest_path = _direct_path(record["archive_manifest"], label="archive manifest", kind="file")
        if os.name == "posix" and stat.S_IMODE(manifest_path.stat().st_mode) != 0o600:
            raise ValueError("archive manifest must have mode 0600")
        if (
            manifest_path.parent.parent != instance.gateway_state_root
            or not manifest_path.parent.name.startswith("runtime-cutover-")
        ):
            raise ValueError(f"archive manifest is outside the instance state root: {instance.instance_id}")
        if _file_sha256(manifest_path) != record.get("archive_manifest_sha256"):
            raise ValueError(f"archive manifest changed: {instance.instance_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        archive = manifest_path.parent
        for item in manifest.get("files", []):
            relative = PurePosixPath(str(item["path"]))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise ValueError(f"archive manifest path escapes its archive: {relative}")
            path = archive.joinpath(*relative.parts)
            kind = item.get("type")
            if kind == "symlink":
                try:
                    info = path.lstat()
                    target = os.readlink(path)
                except OSError as exc:
                    raise ValueError(f"archive symlink is unavailable: {path}") from exc
                encoded = os.fsencode(target)
                if (
                    not stat.S_ISLNK(info.st_mode)
                    or target != item.get("link_target")
                    or len(encoded) != item.get("size")
                    or hashlib.sha256(encoded).hexdigest() != item.get("sha256")
                ):
                    raise ValueError(f"archive symlink changed: {path}")
                continue
            if kind != "file":
                raise ValueError(f"archive manifest file type is unsupported: {path}")
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size != item["size"] or _file_sha256(path) != item["sha256"]:
                raise ValueError(f"archive content changed: {path}")
        marker = json.loads((instance.workspace_root / ".agent-runtime.json").read_text())
        if marker.get("runtime_id") != instance.expected_runtime_id:
            raise ValueError(f"runtime marker does not match inventory: {instance.instance_id}")
        ObservationStore(instance.gateway_state_root)
        if _sqlite_schema(instance.gateway_state_root / "gateway.sqlite3", "gateway_meta") != 3:
            raise ValueError(f"Gateway strict load failed: {instance.instance_id}")
        replay = _controlled_gateway_replay(instance)
        verified.append({"instance_id": instance.instance_id, "runtime_id": instance.expected_runtime_id, **replay})
    return {"schema_version": 1, "inventory_digest": inventory_digest, "receipt": str(receipt_file), "verified": True, "instances": verified}


def _controlled_gateway_replay(instance: CutoverInstance) -> dict[str, Any]:
    """Exercise production Gateway composition through an admission-denied QQ turn."""

    from chatcopilot.evals.agent_case import case_identity, evaluation_cases
    from chatcopilot.evals.gateway_replay import run

    declaration = {
        "schema": "agentstrata.agent-case/v1",
        "title": "Runtime cutover admission replay",
        "input": "cutover verification",
        "expected_behavior": "admission denied before Agent execution",
        "role": "user",
        "channel_kind": "private",
        "runtime_replay": True,
        "admission": "denied",
        "allowed_tools": [],
        "fixtures": {},
        "assertions": [{"kind": "admission_denied"}],
        "semantic": False,
    }
    case = evaluation_cases(
        {"snapshot_id": case_identity(declaration), "case": declaration}
    )[0]
    environment = read_private_env_file(
        instance.private_env, allowed_parent=instance.private_env.parent
    )
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        with tempfile.TemporaryDirectory(prefix="agentstrata-cutover-replay-") as temporary:
            result = run(
                case,
                bot=str(instance.bot_spec),
                workspace_root=Path(temporary),
            )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    evidence = next(
        (
            item.get("runtime_replay")
            for item in result.evidence
            if isinstance(item, dict) and isinstance(item.get("runtime_replay"), dict)
        ),
        None,
    )
    if (
        result.stop_reason != "denied"
        or not isinstance(evidence, dict)
        or evidence.get("production_delivery") is not False
        or evidence.get("layers") != ["gateway"]
    ):
        raise ValueError(f"controlled Gateway replay failed: {instance.instance_id}")
    return {
        "production_delivery": False,
        "gateway_replay": "passed",
        "gateway_layers": evidence["layers"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply", "verify"))
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--plan-digest")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "check":
        result = check_inventory(args.inventory)
    elif args.mode == "apply":
        result = apply_inventory(args.inventory, plan_digest=str(args.plan_digest or ""))
    else:
        if args.receipt is None:
            raise ValueError("verify requires --receipt")
        result = verify_inventory(args.inventory, args.receipt)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
