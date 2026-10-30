"""Install an application source copy and reuse the existing local deployment tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from chatcopilot.core.application_installation import RECORD, SCHEMA, installation
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.source_snapshot import copy_sources, manifest_digest, source_manifest
from chatcopilot.deployment.application_bundle import read_bundle


def _git_environment():
    return {**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "AgentStrata installation", "GIT_AUTHOR_EMAIL": "installation@localhost",
        "GIT_COMMITTER_NAME": "AgentStrata installation", "GIT_COMMITTER_EMAIL": "installation@localhost"}


def _initialize_source(root: Path, names: dict) -> None:
    """Create only this fresh installation's Git objects; never consult hooks or filters."""
    environment = _git_environment()
    def git(*args, data=None):
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *args],
            input=data, capture_output=True, check=True, timeout=60, env=environment)
        return result.stdout.strip()
    git("init", "-q", "--template=")
    git("config", "core.hooksPath", os.devnull)
    git("config", "maintenance.auto", "false")
    entries = []
    for name in sorted(names):
        path = root / name
        blob = git("hash-object", "-w", "--no-filters", "--stdin", data=path.read_bytes())
        mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
        entries.append(mode + b" " + blob + b"\t" + os.fsencode(name) + b"\0")
    git("update-index", "-z", "--index-info", data=b"".join(entries))
    tree = git("write-tree")
    commit = git("commit-tree", tree.decode(), data=b"Installed application source\n")
    git("update-ref", "refs/heads/application", commit.decode())
    git("symbolic-ref", "HEAD", "refs/heads/application")
    (root / ".git/info").mkdir(exist_ok=True)
    (root / ".git/info/exclude").write_text(RECORD + "\n.agentstrata-versions/\n.agentstrata-data/\n.venv/\n")


def install_bundle(bundle: Path, destination: Path) -> dict:
    """Materialize a new local installation without touching any existing application."""
    bundle = bundle.expanduser().absolute()
    record = read_bundle(bundle)
    destination = destination.expanduser().absolute()
    if any(character in str(destination) for character in "\r\n\0"):
        raise ValueError("Application installation path cannot contain control characters")
    if destination.exists() or destination.is_symlink():
        installed = installation(destination)
        if installed is None or installed.bot_id != record["bot_id"] or installed.delivered_digest != record["source_digest"]:
            raise ValueError("Destination already exists; updates must use the accepted-version controls")
        return {"root": str(destination), "bot_id": installed.bot_id, "state": "installed", "reused": True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.resolve() != destination.parent:
        raise ValueError("Application destination contains a symbolic link")
    pending = Path(tempfile.mkdtemp(prefix=".install-application-", dir=destination.parent))
    try:
        copy_sources(bundle / "source", pending, record["source_manifest"])
        materialized = record["source_manifest"]
        _initialize_source(pending, materialized)
        write_private(pending / RECORD, json.dumps({"schema": SCHEMA, "bot_id": record["bot_id"],
            "bot_relative": record["bot_relative"], "delivered_digest": record["source_digest"],
            "installed_digest": manifest_digest(materialized), "accepted_candidate": record["accepted_candidate"]},
            ensure_ascii=False, sort_keys=True) + "\n")
        if source_manifest(pending) != materialized:
            raise ValueError("Installed source differs from its accepted delivery")
        pending.rename(destination)
    finally:
        if pending.exists():
            shutil.rmtree(pending)
    return {"root": str(destination), "bot_id": record["bot_id"], "state": "installed", "reused": False,
        "next_action": "configure the application local.env, then run agentstrata app setup --root " + str(destination)}


def setup_application(root: Path, *, with_console: bool = False, dry_run: bool = False) -> int:
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit application installation is required")
    root = installed.root
    from chatcopilot.core.private_sqlite import private_file
    from chatcopilot.core.settings import load_local_env_values
    registration = Path.home() / ".config/chatcopilot-console" / (installed.bot_id + ".env")
    if registration.exists() or registration.is_symlink():
        private_file(registration)
        values = load_local_env_values(registration, expand_home=True)
        if values.get("CCP_WSL_HOME") != str(root):
            raise ValueError("Another installation owns this application service; its registration is preserved")
    command = ["bash", str(root / "deploy/wsl/update_instance.sh"), "--instance", installed.bot_id,
        "--src", str(root), "--dst", str(root), "--bot", installed.bot_relative]
    if dry_run:
        command.append("--dry-run")
    environment = {**os.environ, "CHATCOPILOT_SOURCE_ROOT": str(root)}
    result = subprocess.run(command, cwd=root, env=environment, check=False)
    if result.returncode or not with_console or dry_run:
        return result.returncode
    return subprocess.run(["bash", str(root / "deploy/wsl/install_wsl_env.sh"), "--no-system-packages",
        "--with-console", "--venv", str(root / ".venv"), "--no-verify"],
        cwd=root, env=environment, check=False).returncode


def prepare_runtime(root: Path) -> int:
    installed = installation(root)
    if installed is None:
        raise ValueError("An explicit application installation is required")
    return subprocess.run(["bash", str(installed.root / "deploy/wsl/install_wsl_env.sh"),
        "--no-system-packages", "--venv", str(installed.root / ".venv"), "--no-verify"],
        cwd=installed.root, env={**os.environ, "CHATCOPILOT_SOURCE_ROOT": str(installed.root)}, check=False).returncode
