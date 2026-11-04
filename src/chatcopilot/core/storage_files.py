"""Handle-based operations used by domain-owned private storage.

POSIX keeps its dir_fd/no-follow semantics. Windows anchors paths to handles,
checks the current-user SID and DACL, and reports that checked access contract in
metadata. This is limited to private application storage, not arbitrary files.
"""
from __future__ import annotations

import os

if os.name != "nt":
    def __getattr__(name):
        if name not in {"O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "open", "stat", "fstat", "mkdir",
                        "unlink", "rmdir", "replace", "link", "fsync", "fchmod", "listdir", "getuid", "geteuid"}:
            raise AttributeError(name)
        return getattr(os, name)

    def lstat(path):
        from pathlib import Path
        return Path(path).lstat()
else:
    from pathlib import Path
    from chatcopilot.core import windows_storage as native

    O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK = native.DIRECTORY, native.NOFOLLOW, 0
    open = native.open_file
    def fstat(fd):
        return native.metadata(descriptor=fd)
    lstat = native.metadata
    mkdir = native.mkdir
    fsync = native.sync
    replace = native.replace

    def getuid():
        return 0

    geteuid = getuid

    def stat(path, *, dir_fd=None, follow_symlinks=False):
        return native.metadata(native.relative(path, dir_fd))

    def fchmod(fd, mode):
        native.tighten(native.path_for_fd(fd))

    def listdir(path):
        return os.listdir(native.api_path(native.path_for_fd(path) if isinstance(path, int) else path))

    def unlink(path, *, dir_fd=None):
        target = native.relative(path, dir_fd)
        native.reject_reparse(target)
        os.unlink(native.api_path(target))

    def rmdir(path, *, dir_fd=None):
        target = native.relative(path, dir_fd)
        native.reject_reparse(target)
        os.rmdir(native.api_path(target))

    def link(source, destination, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=False):
        source, destination = native.relative(source, src_dir_fd), native.relative(destination, dst_dir_fd)
        native.reject_reparse(source)
        native.reject_reparse(destination)
        os.link(native.api_path(source), native.api_path(destination), follow_symlinks=False)


def tighten(path):
    if os.name == "nt":
        native.tighten(Path(path))
    else:
        os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)


def long_path(path):
    from pathlib import Path
    return Path(native.api_path(path)) if os.name == "nt" else Path(path)
