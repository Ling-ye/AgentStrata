#!/usr/bin/env python3
"""Archive protected legacy MEMORY.md files before the record-store cutover.

Dry-run is the default. Apply requires a known inactive user service. The new
runtime ignores both legacy filenames, so archives are never read as memory.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


def _require_offline(unit: str) -> None:
    if not unit or not unit.endswith(".service"):
        raise RuntimeError("--unit must name the stopped Bot systemd user service")
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=LoadState,ActiveState", "--no-pager"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    values = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    if (
        completed.returncode != 0
        or values.get("LoadState") != "loaded"
        or values.get("ActiveState") != "inactive"
    ):
        raise RuntimeError("Bot unit is not proven loaded and inactive; archive refused")


def _validate_dir(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("unsafe protected memory directory")


def _validate_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise RuntimeError("unsafe protected legacy memory file")


def _sha256(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        _validate_file(path)
        digest = hashlib.sha256()
        while chunk := os.read(fd, 65536):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def archive_legacy(root: Path, *, apply: bool) -> list[dict[str, str]]:
    root = Path(root).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise RuntimeError("workspace root must be an absolute direct path")
    root = root.resolve()
    protected = root / ".conversation-state" / "persistent"
    if not protected.exists():
        return []
    for directory in (root / ".conversation-state", protected):
        _validate_dir(directory)
    memory_root = protected / "memory"
    if not memory_root.exists():
        return []
    _validate_dir(memory_root)
    records: list[dict[str, str]] = []
    for scope in ("group", "user"):
        scope_dir = protected / "memory" / scope
        if not scope_dir.exists():
            continue
        _validate_dir(scope_dir)
        for path in sorted(scope_dir.glob("*/MEMORY.md")):
            _validate_dir(path.parent)
            _validate_file(path)
            destination = path.with_name("MEMORY.md.archived")
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("archive destination already exists")
            source_hash = _sha256(path)
            if apply:
                lock_path = path.with_name("MEMORY.md.lock")
                lock_fd = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    if stat.S_IMODE(os.fstat(lock_fd).st_mode) != 0o600:
                        raise RuntimeError("unsafe legacy memory lock")
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    if _sha256(path) != source_hash:
                        raise RuntimeError("legacy memory changed during archive")
                    os.replace(path, destination)
                    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                    if _sha256(destination) != source_hash:
                        raise RuntimeError("archived memory hash mismatch")
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
            records.append({
                "scope": scope, "digest": path.parent.name,
                "sha256": source_hash, "archived": str(destination.relative_to(root)),
            })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--unit", help="stopped Bot systemd user service; required for --apply")
    args = parser.parse_args()
    if args.apply:
        _require_offline(args.unit or "")
    records = archive_legacy(args.workspace_root, apply=args.apply)
    if args.apply:
        _require_offline(args.unit or "")
    print(json.dumps({"applied": args.apply, "records": records}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
