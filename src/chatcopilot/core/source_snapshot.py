"""Content-addressed copies of Git source, excluding runtime and private files."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.private_sqlite import json_text, private_directory
from chatcopilot.core.source_manifest import git_source_paths


def git_output(root: Path, *args: str) -> str:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if result.returncode:
        raise ValueError("Git source operation failed: " + result.stderr.strip()[:400])
    return result.stdout.strip()


def source_manifest(root: Path) -> dict[str, dict[str, Any]]:
    root = root.absolute()
    if root.resolve() != root:
        raise ValueError("source root must be canonical")
    result = {}
    for name in git_source_paths(root):
        if name.endswith(".env") or Path(name).name.startswith(".env"):
            continue
        path = root / name
        size = path.lstat().st_size
        digest = trusted_source_sha256(path, root=root, max_bytes=max(1, size))
        result[name] = {"sha256": digest, "executable": bool(path.stat().st_mode & 0o111)}
    return result


def manifest_digest(manifest: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(json_text(manifest).encode()).hexdigest()


def copy_sources(source: Path, destination: Path, manifest: dict[str, dict[str, Any]]) -> None:
    private_directory(destination)
    for name, expected in manifest.items():
        path = source / name
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("invalid source member")
        digest = trusted_source_sha256(path, root=source, max_bytes=max(1, path.lstat().st_size))
        if digest != expected["sha256"]:
            raise ValueError("source changed while copying")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            content = stream.read()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("source content changed while copying")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o700 if expected["executable"] else 0o600)


def verify_copy(root: Path, manifest: dict[str, dict[str, Any]]) -> None:
    for name, expected in manifest.items():
        path = root / name
        actual = trusted_source_sha256(path, root=root, max_bytes=max(1, path.lstat().st_size))
        if (
            actual != expected["sha256"]
            or bool(path.stat().st_mode & stat.S_IXUSR) != expected["executable"]
        ):
            raise ValueError("frozen source identity changed")
