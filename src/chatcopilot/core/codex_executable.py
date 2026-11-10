"""Resolve installed Codex packages without executing launchers or widening mounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import stat


def resolve_codex_executable(configured: str | Path) -> Path:
    """Return an executable ELF, accepting the official npm entry-point layout.

    Package metadata identifies a layout, not trust or authorization. The caller
    still owns executable identity, credential access and execution permissions.
    Unknown wrappers are never executed to discover a binary or replaced via PATH.
    """
    entry = Path(configured).expanduser().resolve(strict=True)
    if _elf(entry):
        return entry
    root = entry.parent.parent
    if entry != root / "bin/codex.js":
        raise ValueError("Unknown Codex launcher; configure a native Linux executable")
    package = _metadata(root / "package.json")
    if (package.get("name") != "@openai/codex"
            or package.get("bin") != {"codex": "bin/codex.js"}):
        raise ValueError("Codex requires a native Linux executable or the official npm bin/codex.js entry point")
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or machine is None:
        raise ValueError("Unsupported Codex isolation platform or architecture")
    triple = ("x86_64" if machine == "x64" else "aarch64") + "-unknown-linux-musl"
    dependency = "@openai/codex-linux-" + machine
    optional = package.get("optionalDependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("Invalid Codex optional dependency metadata")
    # Match Node's nearest node_modules lookup, including hoisted packages.
    # Resolving a package-directory symlink supports pnpm; payload symlinks may
    # not escape that resolved package. Never fall through a broken installed package.
    for parent in (root, *root.parents):
        if parent.name == "node_modules":
            continue
        candidate = parent / "node_modules" / dependency
        if not candidate.exists() and not candidate.is_symlink():
            continue
        package_root = candidate.resolve(strict=True)
        metadata = _metadata(package_root / "package.json")
        if (dependency not in optional or metadata.get("name") not in (dependency, "@openai/codex")
                or metadata.get("os") != ["linux"] or metadata.get("cpu") != [machine]):
            raise ValueError("Invalid Codex platform package metadata")
        return _payload(package_root, triple)
    # Older official packages bundled vendor beside bin instead of optional deps.
    if (root / "vendor").exists():
        return _payload(root, triple)
    raise ValueError("Missing Codex platform dependency " + dependency + "; reinstall Codex or configure its native executable")


def _metadata(path: Path) -> dict:
    try:
        if not path.resolve(strict=True).is_relative_to(path.parent.resolve()):
            raise ValueError("Codex package metadata escapes its package")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Codex package metadata must be a regular file")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Codex package metadata exceeds size limit")
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ValueError("Invalid or unavailable Codex package metadata") from exc
    if not isinstance(value, dict):
        raise ValueError("Invalid Codex package metadata")
    return value


def _elf(path: Path) -> bool:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("Codex executable must be an executable regular file")
    with path.open("rb") as stream:
        return stream.read(4) == b"\x7fELF"


def _payload(root: Path, triple: str) -> Path:
    binary = (root / "vendor" / triple / "bin/codex").resolve(strict=True)
    if not binary.is_relative_to(root) or not _elf(binary):
        raise ValueError("Codex platform payload must be a native executable inside its package")
    return binary
