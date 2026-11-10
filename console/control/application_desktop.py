"""Explicit per-build desktop bindings; launch only the bytes recorded by the builder."""
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from uuid import UUID

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.private_sqlite import private_file, private_lock
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.core.source_manifest import IGNORED_PARTS, IGNORED_TOP_LEVEL
from chatcopilot.deployment.application_bundle import read_bundle
from studio.delivery import windows_path

SCHEMA = "agentstrata-desktop-launch/v1"


def _hash(path, root):
    return trusted_source_sha256(path, root=root, max_bytes=max(path.stat().st_size, 1))


def build_inputs(applications, ident, message):
    row = applications.require(ident)
    if row["source"] != "studio" or row["type"] not in {"desktop", "companion"}:
        raise PermissionError("This application has no desktop launcher")
    message = UUID(str(message)).hex
    studio = applications.studio()
    artifact = studio.artifact(row["project_id"], message)
    stage = artifact.parent
    receipt = json.loads((stage / "build-receipt.json").read_text())
    expected = receipt.get("launcher_manifest_sha256")
    if not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
        raise ValueError("This older build has no trusted launcher manifest; use the installed client")
    manifest_path = stage / "desktop-launch.json"
    if _hash(manifest_path, stage) != expected:
        raise ValueError("Desktop launcher manifest changed")
    launch = json.loads(manifest_path.read_text())
    install_id = receipt.get("install_id", "")
    if (not re.fullmatch("[a-z][a-z0-9-]{1,80}", install_id)
            or launch.get("schema") != SCHEMA or launch.get("install_id") != install_id
            or launch.get("executable") != install_id + ".exe"):
        raise ValueError("Invalid desktop launch identity")
    files = launch.get("files")
    if not isinstance(files, dict) or not {launch["executable"], "resources/app.asar"} <= files.keys():
        raise ValueError("Incomplete desktop launcher manifest")
    folded = set()
    for name, digest in files.items():
        path = PurePosixPath(name)
        if (not name or path.as_posix() != name or path.is_absolute() or ".." in path.parts
                or any(c in name for c in '\\:\x00\r\n') or name.casefold() in folded
                or not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest)):
            raise ValueError("Invalid desktop launcher member")
        folded.add(name.casefold())
    payload = read_bundle(stage / "runtime-payload")
    for key in ("source_digest", "artifacts_digest", "bot_id", "bot_relative"):
        if payload[key] != receipt[key]:
            raise ValueError("Build runtime identity changed")
    if manifest_digest(payload["source_manifest"]) != receipt["source_digest"]:
        raise ValueError("Build source identity changed")
    with studio.db.connect() as db:
        project = db.execute("SELECT bot FROM projects WHERE id=?", (row["project_id"],)).fetchone()
    if project is None or project["bot"] != receipt["bot_id"]:
        raise ValueError("Desktop build belongs to another application")
    # The native validator consumes trusted build data, never a manifest from the selected EXE.
    return row, message, {"launcher": launch, "source_manifest": payload["source_manifest"],
                         **{key: receipt[key] for key in ("source_digest", "artifacts_digest", "bot_id", "bot_relative")},
                         "ignored_parts": sorted(IGNORED_PARTS), "ignored_top": sorted(IGNORED_TOP_LEVEL)}


def _native(applications, mode, inputs, executable=None):
    script = applications.root / "integrations/desktop_app/launch.ps1"
    _hash(script, applications.root)
    with tempfile.TemporaryDirectory(prefix="agentstrata-launch-") as temp:
        request = Path(temp) / "request.json"
        write_private(request, json.dumps(inputs))
        command = ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                   "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                   "-File", windows_path(script), "-Mode", mode, "-RequestFile", windows_path(request)]
        if executable is not None:
            command.extend(["-Executable", executable])
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True,
                                    encoding="utf-8", timeout=300 if mode == "select" else 120)
            value = json.loads(result.stdout.lstrip("\ufeff"))
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise ValueError("Desktop selection or validation failed; no launch success is confirmed") from exc
    if (not isinstance(value, dict) or value.get("state") != ("bound" if mode == "select" else "requested")
            or not isinstance(value.get("executable"), str) or not value["executable"]):
        raise ValueError("Invalid native launch response")
    return value


def operate(applications, ident, message, action):
    if action not in {"bind", "launch"}:
        raise ValueError("Invalid desktop operation")
    row, message, inputs = build_inputs(applications, ident, message)
    studio = applications.studio()
    directory = studio.directory(row["project_id"])
    binding_file = directory / ("desktop-binding-" + message + ".json")
    script = applications.root / "integrations/desktop_app/launch.ps1"
    identity = {"application_id": ident, "message_id": message,
                "manifest_digest": manifest_digest(inputs["launcher"]["files"]),
                "script_digest": _hash(script, applications.root)}
    with private_lock(directory / "desktop-launch.lock", timeout=1):
        if action == "bind":
            native = _native(applications, "select", inputs)
            write_private(binding_file, json.dumps({**identity, "executable": native["executable"]}))
            return {"state": "bound", "message": "Installed executable verified and bound to this build."}
        if not binding_file.exists():
            raise ValueError("Select and verify this build's installed executable first")
        private_file(binding_file)
        binding = json.loads(binding_file.read_text())
        if any(binding.get(key) != value for key, value in identity.items()):
            raise ValueError("Desktop binding changed; select the installed executable again")
        # The installer receipt binds the host/payload, not the prepared Python, wheels,
        # plugin environments or MCP binaries. Never turn local TOFU into build evidence.
        raise ValueError("Desktop launch unavailable: prepared runtime dependencies lack independently trusted integrity evidence")
