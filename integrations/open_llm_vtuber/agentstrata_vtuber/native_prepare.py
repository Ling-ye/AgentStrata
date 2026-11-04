"""Install prebuilt public client inputs; Windows users do not build upstream code."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from chatcopilot.core.application_installation import installation
from chatcopilot.core.install_artifacts import InstallError, private_directory, sha256
from .application_binding import configure_application_client
from .assets import prepare_assets
from .prepare import prepare_models, verify_receipt
from .resources import artifact_lock, manifest, resource


def prepare(request: dict) -> dict:
    root = Path(request["root"])
    installed = installation(Path(request["host"]["application"]["root"]))
    payload = Path(request["artifacts"])
    if installed is None or sha256(payload / "artifacts.json") != installed.artifact_digest:
        raise InstallError("Native artifact manifest differs from the installed application")
    record = json.loads((payload / "artifacts.json").read_text())
    if installed is None or record.get("source_digest") != installed.delivered_digest:
        raise InstallError("Native artifacts belong to another application source")
    for name, digest in record["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise InstallError("Invalid native artifact path")
        path = payload / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise InstallError("Native artifact content changed")
    sources = {}
    for name, pin in manifest().items():
        source = payload / "vtuber/apps" / name
        receipt = json.loads((source / ".agentstrata-source.json").read_text())
        if (receipt.get("revision") != pin["revision"]
                or receipt.get("archive_sha256") != artifact_lock()[name]["sha256"]
                or receipt.get("patch_sha256") != sha256(Path(str(resource(pin["patch"]))))
                or not verify_receipt(source, receipt)):
            raise InstallError("Native upstream sources differ from the fixed application inputs")
        destination = root / "runtime/apps" / name
        if destination.exists():
            previous = json.loads((destination / ".agentstrata-source.json").read_text())
            if previous != receipt or not verify_receipt(destination, previous):
                raise InstallError("Installed upstream source changed")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            pending = destination.with_name(destination.name + ".preparing")
            if pending.is_symlink():
                raise InstallError("Native preparation directory cannot be a symbolic link")
            if pending.exists():
                shutil.rmtree(pending)
            shutil.copytree(source, pending)
            pending.rename(destination)
        sources[name] = destination
    # ffmpeg is provisioned from a fixed native artifact by the common bootstrap.
    executable = root / "runtime/tools/ffmpeg/bin/ffmpeg.exe"
    lock = json.loads((installed.root / "integrations/live2d_desktop/windows-runtime.lock.json").read_text())
    if not executable.is_file() or sha256(executable) != lock["ffmpeg"]["binary_sha256"]:
        raise InstallError("Native ffmpeg is not prepared")
    private_directory(root / "data")
    configure_application_client(request, sources)
    prepare_assets(root, sources["backend"])
    prepare_models(root)
    return {"ok": True, "platform": "windows-x86_64"}
