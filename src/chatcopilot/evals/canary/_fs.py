"""Private filesystem helpers shared by Canary primitives.

These helpers deliberately support only current-user, mode-0700 directories and
mode-0600 regular files below a pre-created private root. They do not attempt to
turn an arbitrary deployment directory into a Canary target.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any

from .errors import CanarySafetyError


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def absolute_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CanarySafetyError(f"Canary paths must be absolute: {path}")
    return Path(os.path.abspath(os.fspath(path)))


def paths_overlap(left: Path, right: Path) -> bool:
    left_text = os.fspath(left)
    right_text = os.fspath(right)
    try:
        common = os.path.commonpath((left_text, right_text))
    except ValueError:
        return False
    return common in {left_text, right_text}


def ensure_contained(path: Path, root: Path, *, allow_equal: bool = False) -> None:
    path = absolute_path(path)
    root = absolute_path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise CanarySafetyError(f"path escapes private Canary root: {path}") from exc
    if not allow_equal and relative == Path("."):
        raise CanarySafetyError("operation requires a path below the private Canary root")


def validate_private_directory(
    path: Path,
    *,
    root: Path | None = None,
    expected_uid: int | None = None,
) -> os.stat_result:
    path = absolute_path(path)
    if root is not None:
        root = absolute_path(root)
        ensure_contained(path, root, allow_equal=True)
        validate_private_directory(root, expected_uid=expected_uid)
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            _validate_one_directory(current, expected_uid=expected_uid)
        return os.lstat(path)
    return _validate_one_directory(path, expected_uid=expected_uid)


def _validate_one_directory(path: Path, *, expected_uid: int | None) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise CanarySafetyError(f"private Canary directory is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CanarySafetyError(f"private Canary path is not a real directory: {path}")
    uid = os.getuid() if expected_uid is None else expected_uid
    if info.st_uid != uid:
        raise CanarySafetyError(f"private Canary directory has the wrong owner: {path}")
    if stat.S_IMODE(info.st_mode) != PRIVATE_DIR_MODE:
        raise CanarySafetyError(f"private Canary directory must use mode 0700: {path}")
    return info


def validate_private_file(
    path: Path,
    *,
    root: Path,
    expected_uid: int | None = None,
    mode: int = PRIVATE_FILE_MODE,
) -> os.stat_result:
    path = absolute_path(path)
    root = absolute_path(root)
    ensure_contained(path, root)
    validate_private_directory(path.parent, root=root, expected_uid=expected_uid)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise CanarySafetyError(f"private Canary file is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CanarySafetyError(f"private Canary path is not a regular file: {path}")
    uid = os.getuid() if expected_uid is None else expected_uid
    if info.st_uid != uid:
        raise CanarySafetyError(f"private Canary file has the wrong owner: {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise CanarySafetyError(f"private Canary file has unsafe mode: {path}")
    if info.st_nlink != 1:
        raise CanarySafetyError(f"private Canary file must have one hard link: {path}")
    return info


def make_private_directory(path: Path, *, root: Path, expected_uid: int | None = None) -> None:
    ensure_contained(path, root)
    validate_private_directory(path.parent, root=root, expected_uid=expected_uid)
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE)
    except OSError as exc:
        raise CanarySafetyError(f"failed to create private Canary directory: {path}") from exc
    validate_private_directory(path, root=root, expected_uid=expected_uid)


def write_new_private_file(path: Path, data: bytes, *, root: Path) -> None:
    ensure_contained(path, root)
    validate_private_directory(path.parent, root=root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise CanarySafetyError(f"failed to create private Canary file: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    validate_private_file(path, root=root)


def atomic_write_private_json(path: Path, payload: dict[str, Any], *, root: Path) -> None:
    ensure_contained(path, root)
    validate_private_directory(path.parent, root=root)
    if path.exists() or path.is_symlink():
        validate_private_file(path, root=root)
    temporary = path.parent / f".{path.name}.tmp-{secrets.token_hex(8)}"
    encoded = canonical_json(payload) + b"\n"
    write_new_private_file(temporary, encoded, root=root)
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    validate_private_file(path, root=root)


def write_new_private_json(path: Path, payload: dict[str, Any], *, root: Path) -> None:
    """Create a new private JSON file without replacing any existing identity."""

    write_new_private_file(path, canonical_json(payload) + b"\n", root=root)


def read_private_json(path: Path, *, root: Path) -> dict[str, Any]:
    validate_private_file(path, root=root)
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
                or info.st_nlink != 1
            ):
                raise CanarySafetyError(f"private Canary file changed during open: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
                descriptor = -1
                payload = json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanarySafetyError(f"private Canary JSON is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise CanarySafetyError(f"private Canary JSON must be an object: {path}")
    return payload


def canonical_json(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanarySafetyError("Canary payload is not canonical JSON") from exc


__all__ = [
    "PRIVATE_DIR_MODE",
    "PRIVATE_FILE_MODE",
    "absolute_path",
    "atomic_write_private_json",
    "canonical_json",
    "ensure_contained",
    "make_private_directory",
    "paths_overlap",
    "read_private_json",
    "validate_private_directory",
    "validate_private_file",
    "write_new_private_file",
    "write_new_private_json",
]
