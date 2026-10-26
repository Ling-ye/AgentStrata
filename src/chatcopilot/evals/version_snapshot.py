"""Content binding for source-backed Evaluations, not an acceptance receipt."""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import platform
import shlex
import sys

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.file_integrity import require_regular_file
from chatcopilot.evals.code_source import read_source_receipt, verify_code_source

SCHEMA = "agentstrata-evaluation-version/v1"


def managed_source(output: Path):
    """Use the service-owned request after Core's managed-artifact validation.

    An environment variable or adapter metadata is never sufficient authority.
    """
    from chatcopilot.core.private_sqlite import private_file
    path = output / "request.json"
    private_file(path)
    request = json.loads(path.read_text())
    if not request.get("code_source"):
        return None
    receipt = read_source_receipt(output.parent / ".sources" / f"{output.name}.json")
    if {k: v for k, v in receipt.items() if k != "manifest"} != request["code_source"]:
        raise ValueError("Evaluation source receipt does not match its managed request")
    return receipt


def runtime_identity(targets, model_config=None) -> dict:
    packages = {}
    for name in ("langgraph", "langgraph-checkpoint", "mcp", "websockets", "uvicorn", "deepeval"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    identity = {"python": sys.version.split()[0], "platform": platform.system(),
                "machine": platform.machine(), "packages": packages}
    if any(t.runtime_id == "codex" and t.executor in {"agent_configured", "agent_isolated"} for t in targets):
        if model_config is None:
            raise ValueError("Codex version binding requires resolved model configuration")
        from chatcopilot.external_tools.codex_cli.command import build_app_server_command
        command = build_app_server_command(
            (shlex.quote(model_config.codex_bin) if model_config.codex_bin else "codex") + " exec --model {model} --cd {workdir}",
            model=model_config.model, workdir=Path.cwd(), reasoning_effort=model_config.reasoning_effort or "",
            web_search_mode="disabled", shell_env_overrides=None, extra_config=(),
        )
        path = Path(command[0]).resolve(strict=True)
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            require_regular_file(before)
            magic = stream.read(4)
            if not (magic == b"\x7fELF" or magic[:2] == b"MZ"):
                raise ValueError("Codex version binding requires its native executable, not a launcher script")
            stream.seek(0)
            value = hashlib.file_digest(stream, "sha256").hexdigest()
            after = os.fstat(stream.fileno())
        def fields(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if fields(before) != fields(after) or fields(after) != fields(path.stat()):
            raise ValueError("Codex executable changed while recording its version")
        identity["codex_executable_sha256"] = value
    return identity


def capture(source, targets, *, model_config=None, product_components=None) -> dict:
    verify_code_source(source)
    active_root = Path(__file__).resolve().parents[3]
    if Path(source["path"]) != active_root:
        raise ValueError("version snapshot is not the executing source tree")
    protected = {name: row for name, row in source["manifest"].items()
                 if name.startswith("src/chatcopilot/evals/") or name in {"pyproject.toml", "uv.lock"}}
    payload = {
        "schema": SCHEMA,
        "source_sha256": source["sha256"],
        "execution_sha256": source["execution_sha256"],
        "verification_sha256": digest(protected),
        "targets": {t.target_id: t.fingerprint for t in targets},
        "runtime": runtime_identity(targets, model_config),
        "product_components": product_components or {},
    }
    return {**payload, "snapshot_ref": digest(payload)}


def validate(snapshot, targets) -> None:
    if (not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA
            or set(snapshot) != {"schema", "source_sha256", "execution_sha256", "verification_sha256",
                                 "targets", "runtime", "product_components", "snapshot_ref"}
            or not isinstance(snapshot.get("runtime"), dict) or not snapshot["runtime"]
            or snapshot.get("snapshot_ref") != digest({k: v for k, v in snapshot.items() if k != "snapshot_ref"})
            or snapshot.get("targets") != {t["target_id"]: t["fingerprint"] for t in targets}):
        raise ValueError("invalid Evaluation version snapshot")
    for key in ("source_sha256", "execution_sha256", "verification_sha256"):
        value = snapshot.get(key, "")
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("incomplete Evaluation source binding")


def verify(source, snapshot, targets, model_config=None) -> None:
    verify_code_source(source)
    if runtime_identity(targets, model_config) != snapshot["runtime"]:
        raise ValueError("Evaluation runtime changed during execution")
