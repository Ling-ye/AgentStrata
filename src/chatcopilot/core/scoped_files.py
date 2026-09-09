"""Descriptor-relative ordinary file access within a host-bound resource."""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from uuid import uuid4

from chatcopilot.contracts.execution_scope import current_execution_scope


@contextmanager
def _parent(path: Path, *, write: bool = False, create: bool = False):
    scope = current_execution_scope()
    if scope is None or not scope.permits(path, write=write):
        raise PermissionError("file is outside the bound execution resources")
    # Walk the original path without following a link between validation and IO.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.absolute().parts[1:-1]:
            if name in {".", ".."}:
                raise PermissionError("non-canonical file path")
            if create:
                try:
                    os.mkdir(name, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError("ordinary files must be regular and single-link")


def read_bytes(path: Path, limit: int) -> bytes:
    if current_execution_scope() is None:
        with path.open("rb") as stream:
            return stream.read(limit)
    with _parent(path) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            _regular(os.fstat(stream.fileno()))
            return stream.read(limit)


def read_text(path: Path) -> str:
    if current_execution_scope() is None:
        return path.read_text(encoding="utf-8", errors="replace")
    with _parent(path) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, encoding="utf-8", errors="replace") as stream:
            _regular(os.fstat(stream.fileno()))
            return stream.read()


def write_text(path: Path, content: str) -> None:
    if current_execution_scope() is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return
    with _parent(path, write=True, create=True) as parent:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            mode = 0o600
        else:
            _regular(info)
            mode = stat.S_IMODE(info.st_mode)
        temporary = ".write-" + uuid4().hex
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def delete_file(path: Path) -> None:
    if current_execution_scope() is None:
        path.unlink()
        return
    with _parent(path, write=True) as parent:
        _regular(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
        os.unlink(path.name, dir_fd=parent)
