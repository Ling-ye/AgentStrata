"""Small filesystem primitives shared by domain-owned storage boundaries."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat


class FileMetadataError(PermissionError):
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def require_regular_file(
    metadata: os.stat_result,
    *,
    owner_uid: int | None = None,
    mode: int | None = None,
    single_link: bool = False,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise FileMetadataError("type", "file must be a regular file")
    if owner_uid is not None and metadata.st_uid != owner_uid:
        raise FileMetadataError("owner", "file must be owned by the current user")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise FileMetadataError("mode", f"file must use mode {mode:04o}")
    if single_link and metadata.st_nlink != 1:
        raise FileMetadataError("links", "file must have exactly one hard link")


def _content_identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def trusted_source_sha256(path: Path, *, root: Path, max_bytes: int) -> str:
    """Hash one package source while its directory entries and bytes stay stable."""
    path = path.absolute()
    root = root.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("source is outside trusted package") from exc
    if not relative.parts or ".." in relative.parts:
        raise ValueError("source is outside trusted package")
    parents: list[tuple[Path, os.stat_result]] = []
    current = root
    for part in (None, *relative.parts[:-1]):
        if part is not None:
            current = current / part
        info = current.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("source parent is unsafe")
        parents.append((current, info))
    before = path.stat(follow_symlinks=False)
    try:
        require_regular_file(before, single_link=True)
    except FileMetadataError as exc:
        raise ValueError("source inode is unsafe") from exc
    if before.st_size > max_bytes:
        raise ValueError("source is too large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _content_identity(opened) != _content_identity(before):
            raise ValueError("source changed before reading")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("source is too large")
            digest.update(chunk)
        finished = os.fstat(descriptor)
        after = path.stat(follow_symlinks=False)
        if (total != opened.st_size or _content_identity(finished) != _content_identity(opened)
                or _content_identity(after) != _content_identity(opened)):
            raise ValueError("source changed while reading")
        for parent, original in parents:
            latest = parent.stat(follow_symlinks=False)
            if (not stat.S_ISDIR(latest.st_mode)
                    or (latest.st_dev, latest.st_ino, latest.st_mtime_ns, latest.st_ctime_ns)
                    != (original.st_dev, original.st_ino, original.st_mtime_ns, original.st_ctime_ns)):
                raise ValueError("source parent changed while reading")
        return digest.hexdigest()
    finally:
        os.close(descriptor)
