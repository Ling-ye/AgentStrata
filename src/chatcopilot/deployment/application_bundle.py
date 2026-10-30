"""Portable, content-bound application sources using the existing release input."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, verify_copy
from chatcopilot.contracts.deployment import ReleaseSource


SCHEMA = "agentstrata-application-bundle/v1"
MANIFEST = "application.json"


def read_bundle(root: Path) -> dict:
    root = root.expanduser().absolute()
    if root.resolve() != root or (root / MANIFEST).is_symlink():
        raise ValueError("Application bundle must contain ordinary files at a canonical path")
    value = json.loads((root / MANIFEST).read_text())
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("Unsupported application bundle")
    manifest = value.get("source_manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("Application bundle has no source manifest")
    for name, row in manifest.items():
        if (not isinstance(name, str) or "\\" in name or PurePosixPath(name).as_posix() != name
                or not is_deployable_source_path(name) or not isinstance(row, dict)
                or set(row) != {"sha256", "executable"} or type(row["executable"]) is not bool
                or not isinstance(row["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])):
            raise ValueError("Application bundle contains an invalid or private source member")
    identity = manifest_digest(manifest)
    receipt = value.get("accepted_candidate", {})
    if (not isinstance(receipt, dict) or value.get("source_digest") != identity or receipt.get("candidate_digest") != identity
            or not all(receipt.get(key) for key in ("goal_digest", "verification_digest", "review_binding"))):
        raise ValueError("Application bundle does not bind its accepted source")
    source = root / "source"
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Application bundle source is unavailable")
    members = set()
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Application bundle cannot contain symbolic links")
        if path.is_file():
            members.add(path.relative_to(source).as_posix())
    if members != set(manifest):
        raise ValueError("Application bundle contains missing or unrecorded files")
    verify_copy(source, manifest)
    relative = value.get("bot_relative")
    if not isinstance(relative, str) or relative not in manifest or not relative.startswith("bots/"):
        raise ValueError("Application bundle does not identify its application")
    app = load_application_spec(source / relative)
    if (app.raw.get("deploy", {}).get("instance_id") or app.raw.get("id")) != value.get("bot_id"):
        raise ValueError("Application bundle identity differs from its configuration")
    if not isinstance(app.raw.get("gateway"), dict):
        raise ValueError("Independent application delivery requires the Gateway runtime")
    required = {"pyproject.toml", "uv.lock", "src/chatcopilot/__main__.py", "src/chatcopilot/application_cli.py",
                "deploy/wsl/install_wsl_env.sh", "deploy/wsl/update_instance.sh"}
    if not required.issubset(manifest):
        raise ValueError("Application source does not include the independent installation entrypoints")
    return value


def export_bundle(source: ReleaseSource, output: Path) -> dict:
    """Export exact accepted files; no dependency on the original task remains."""
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        existing = read_bundle(output)
        if (existing["source_digest"] != manifest_digest(source.candidate_manifest) or existing["bot_id"] != source.bot_id
                or existing["accepted_candidate"] != source.receipt
                or existing.get("runtime_environment") != source.runtime_environment
                or existing.get("model_identity") != source.model_identity):
            raise ValueError("Output already contains another application delivery")
        return {"path": str(output), **existing}
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.resolve() != output.parent:
        raise ValueError("Application output directory contains a symbolic link")
    pending = Path(tempfile.mkdtemp(prefix=".application-", dir=output.parent))
    try:
        copy_sources(source.candidate, pending / "source", source.candidate_manifest)
        record = {"schema": SCHEMA, "bot_id": source.bot_id, "bot_relative": source.bot_relative,
            "source_digest": manifest_digest(source.candidate_manifest), "source_manifest": source.candidate_manifest,
            "accepted_candidate": source.receipt, "runtime_environment": source.runtime_environment,
            "model_identity": source.model_identity}
        write_private(pending / MANIFEST, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        script = """#!/usr/bin/env bash
set -euo pipefail
BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agentstrata-install.XXXXXX")"
trap 'rm -rf -- "$INSTALLER_ROOT"' EXIT
cp -R "$BUNDLE_ROOT/source/." "$INSTALLER_ROOT/"
bash "$INSTALLER_ROOT/deploy/wsl/install_wsl_env.sh" --no-system-packages --no-verify --venv "$INSTALLER_ROOT/.venv"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$INSTALLER_ROOT/src" "$INSTALLER_ROOT/.venv/bin/python" -m chatcopilot app install "$BUNDLE_ROOT" "$@"
"""
        write_private(pending / "install.sh", script)
        (pending / "install.sh").chmod(0o700)
        read_bundle(pending)
        pending.rename(output)
        return {"path": str(output), **record}
    finally:
        if pending.exists():
            shutil.rmtree(pending)
