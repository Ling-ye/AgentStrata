"""Explicit installed-application identity and its existing managed source selection."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re

from chatcopilot.core.application_configuration import load_application_spec
from chatcopilot.core.private_sqlite import private_file
from chatcopilot.core.source_snapshot import manifest_digest, source_manifest, verify_copy


RECORD = ".agentstrata-application.json"
SCHEMA = "agentstrata-application-installation/v1"


@dataclass(frozen=True)
class InstalledApplication:
    root: Path
    bot_id: str
    bot_relative: str
    delivered_digest: str

    @property
    def control(self) -> Path:
        return self.root / ".agentstrata-versions"

    def deployment_overrides(self) -> dict[str, str]:
        """Machine locations are host inputs, never edits to the delivered BotSpec."""
        return {"instance_id": self.bot_id, "wsl_home": str(self.root),
                "workspace_root": str(self.root / ".agentstrata-data/workspace"),
                "log_dir": str(self.root / ".agentstrata-data/logs"), "env_file": str(self.control / "runtime.env")}


def installation(root: Path) -> InstalledApplication | None:
    """A missing record means an ordinary checkout; an invalid record is an error."""
    root = root.expanduser().absolute()
    path = root / RECORD
    if not path.exists() and not path.is_symlink():
        return None
    if root.resolve() != root or root.stat().st_uid != os.getuid():
        raise ValueError("Application installation must be owned by this user and contain no symlink")
    private_file(path)
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("Unsupported application installation record")
    bot_id, relative, delivered = (value.get(key, "") for key in ("bot_id", "bot_relative", "delivered_digest"))
    if (not isinstance(bot_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", bot_id)
            or not isinstance(relative, str) or PurePosixPath(relative).as_posix() != relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
            or not relative.startswith("bots/") or not relative.endswith("/bot.yaml")
            or not isinstance(delivered, str) or not re.fullmatch(r"[a-f0-9]{64}", delivered)):
        raise ValueError("Invalid application installation identity")
    app = load_application_spec(root / relative)
    if (app.raw.get("deploy", {}).get("instance_id") or app.raw.get("id")) != bot_id:
        raise ValueError("Installed application declaration changed identity")
    return InstalledApplication(root, bot_id, relative, delivered)


def application_source(root: Path, bot_id: str | None = None) -> tuple[Path, dict] | None:
    """Resolve the current explicit installation, preserving M5's immutable selection."""
    installed = installation(root)
    if installed is None:
        return None
    if bot_id is not None and bot_id != installed.bot_id:
        raise ValueError("The requested application does not belong to this installation")
    pointer = installed.control / "current.json"
    if pointer.exists() or pointer.is_symlink():
        from chatcopilot.core.release_identity import read_json
        from chatcopilot.core.release_runtime import validate_selection
        selected = read_json(pointer)
        source = validate_selection(installed.control, selected)
        if selected.get("bot_relative") != installed.bot_relative:
            raise ValueError("Selected version belongs to another application")
        manifest = read_json(source.parent / "manifest.json")
    else:
        source = installed.root
        manifest = source_manifest(source)
    verify_copy(source, manifest)
    return source, manifest


def application_identity(root: Path, bot_id: str | None = None) -> dict | None:
    installed = installation(root)
    if installed is None:
        return None
    selected = application_source(root, bot_id)
    if selected is None:
        raise ValueError("Application installation changed while resolving its version")
    source, manifest = selected
    return {"bot_id": installed.bot_id, "source_digest": manifest_digest(manifest),
            "delivered_digest": installed.delivered_digest, "source_root": str(source),
            "bot_relative": installed.bot_relative}


def running_application_identity(source: Path, control: Path | None = None) -> dict | None:
    """Capture the code actually loaded by this process, not a later version pointer."""
    root = control.parent if control is not None else source
    installed = installation(root)
    if installed is None:
        return None
    if source == root:
        manifest = source_manifest(source)
    else:
        resolved = application_source(root)
        if resolved is None or source != resolved[0]:
            raise ValueError("Running application source differs from its selected version")
        _, manifest = resolved
    return {"bot_id": installed.bot_id, "source_digest": manifest_digest(manifest)}
