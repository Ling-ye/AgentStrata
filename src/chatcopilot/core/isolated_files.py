"""Private, exclusive new files anchored beneath a checked isolated workspace.

No object is replaced, repaired, renamed or deleted. Windows holds every ancestor
without write/delete sharing and uses a stable volume namespace. POSIX uses
dir_fd and no-follow operations. All created file handles remain open to the end.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from chatcopilot.core.install_artifacts import InstallError


@dataclass
class _Ref:
    handle: Any
    path: Any
    parent: _Ref | None
    name: str | None
    directory: bool
    private: bool
    identity: tuple[int, ...]


class _Posix:
    def __init__(self):
        self.directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def info(self, ref):
        value = os.fstat(ref.handle)
        if stat.S_ISDIR(value.st_mode) != ref.directory or (not ref.directory and not stat.S_ISREG(value.st_mode)):
            raise InstallError("Isolated storage entry has an unsafe type")
        if ref.private and (value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != (0o700 if ref.directory else 0o600)):
            raise InstallError("Isolated storage requires current ownership and private permissions")
        if not ref.directory and value.st_nlink != 1:
            raise InstallError("Isolated storage file has multiple links")
        return (value.st_dev, value.st_ino), (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns), value.st_size

    def linked(self, ref):
        if ref.parent is None:
            return self.info(ref)[0]
        value = os.stat(ref.name, dir_fd=ref.parent.handle, follow_symlinks=False)
        if not (stat.S_ISDIR(value.st_mode) if ref.directory else stat.S_ISREG(value.st_mode)):
            raise InstallError("Isolated storage pathname changed type")
        return value.st_dev, value.st_ino

    def root(self, path):
        return os.open(path, self.directory_flags), Path(path)

    def directory(self, parent, name, create):
        if create:
            os.mkdir(name, mode=0o700, dir_fd=parent.handle)
        return os.open(name, self.directory_flags, dir_fd=parent.handle), parent.path / name

    def file(self, parent, name, create):
        flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | (os.O_RDWR if create else os.O_RDONLY)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        handle = os.open(name, flags, 0o600, dir_fd=parent.handle)
        try:
            if create:
                value = os.fstat(handle)
                if not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or value.st_nlink != 1:
                    raise InstallError("New isolated file identity is unsafe")
                os.fchmod(handle, 0o600)
            return handle, parent.path / name
        except BaseException:
            os.close(handle)
            raise

    def names(self, ref):
        return set(os.listdir(ref.handle))

    def write(self, ref, data):
        return os.write(ref.handle, data)

    def read(self, ref, size):
        return os.read(ref.handle, size)

    def rewind(self, ref):
        os.lseek(ref.handle, 0, os.SEEK_SET)

    def flush(self, ref):
        os.fsync(ref.handle)

    def close(self, ref):
        os.close(ref.handle)


class _Windows:
    def __init__(self):
        import win32con
        import win32file
        from chatcopilot.core import windows_storage
        self.constants, self.files, self.native = win32con, win32file, windows_storage

    def _open(self, path, *, create=False, file=False, metadata=False):
        c = self.constants
        access = c.READ_CONTROL | (0x0080 if metadata else c.GENERIC_READ)  # FILE_READ_ATTRIBUTES
        if create and file:
            access |= c.GENERIC_WRITE
        share = c.FILE_SHARE_READ | c.FILE_SHARE_WRITE | c.FILE_SHARE_DELETE if metadata else (0 if file and create else c.FILE_SHARE_READ)
        try:
            return self.files.CreateFile(path, access, share, self.native.security_attributes() if create else None,
                c.CREATE_NEW if create else c.OPEN_EXISTING, c.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000, None)
        except self.native.pywintypes.error as error:
            raise self.native._error(error) from None

    def info(self, ref):
        directory = self.native._validate(ref.handle, private=ref.private, single_link=not ref.directory)
        if directory != ref.directory:
            raise InstallError("Isolated storage entry has an unsafe type")
        value = self.files.GetFileInformationByHandle(ref.handle)
        identity = (value[4], value[8], value[9])
        size = (value[5] << 32) | value[6]
        return identity, (identity, value[0], value[1], value[3], value[7], size), size

    def linked(self, ref):
        if ref.parent is None:
            return self.info(ref)[0]
        handle = self._open(ref.path, metadata=True)
        try:
            other = _Ref(handle, ref.path, None, None, ref.directory, ref.private, ())
            return self.info(other)[0]
        finally:
            handle.Close()

    def root(self, path):
        if self.files.GetDriveType(str(path)) != self.constants.DRIVE_FIXED:
            raise InstallError("Isolated workspace requires a fixed local Windows volume")
        handle = self._open("\\\\?\\" + str(path))
        try:
            volume = self.files.GetFinalPathNameByHandle(handle, 1)
            if not re.fullmatch(r"\\\\\?\\Volume\{[0-9a-fA-F-]+\}\\", volume):
                raise InstallError("Isolated workspace volume identity is unsupported")
            return handle, volume.rstrip("\\")
        except BaseException:
            handle.Close()
            raise

    def directory(self, parent, name, create):
        path = parent.path + "\\" + name
        if create:
            try:
                self.files.CreateDirectory(path, self.native.security_attributes())
            except self.native.pywintypes.error as error:
                raise self.native._error(error) from None
        return self._open(path), path

    def file(self, parent, name, create):
        path = parent.path + "\\" + name
        return self._open(path, create=create, file=True), path

    def names(self, ref):
        return set(os.listdir(ref.path))

    def write(self, ref, data):
        return self.files.WriteFile(ref.handle, data)[1]

    def read(self, ref, size):
        return self.files.ReadFile(ref.handle, size)[1]

    def rewind(self, ref):
        self.files.SetFilePointer(ref.handle, 0, self.constants.FILE_BEGIN)

    def flush(self, ref):
        self.files.FlushFileBuffers(ref.handle)

    def close(self, ref):
        ref.handle.Close()


class IsolatedTree:
    """Internal writer capability; callers must first validate independent inputs.

    Existing workspace metadata is only a write-confinement precondition. It is
    never used to establish the code's provenance or authorize execution.
    """
    def __init__(self, workspace: Path, *, create: bool):
        self.view = Path(workspace)
        if not self.view.is_absolute() or ".." in self.view.parts:
            raise InstallError("Isolated workspace must be an explicit absolute path")
        if os.name == "nt" and not re.fullmatch(r"[A-Za-z]:\\", self.view.anchor):
            raise InstallError("Isolated workspace requires a local Windows drive path")
        self.backend = _Windows() if os.name == "nt" else _Posix()
        self.refs: list[_Ref] = []
        self.nodes: dict[str, _Ref] = {}
        self.workspace = self.root = None
        self.stage_path = None
        self.stage_name = None
        try:
            handle, path = self.backend.root(Path(self.view.anchor))
            current = self._keep(handle, path, None, None, True, False)
            for part in self.view.parts[1:]:
                handle, path = self.backend.directory(current, part, False)
                current = self._keep(handle, path, current, part, True, False)
            current.private = True
            self._check(current)
            self.workspace = current
            if create:
                if self.backend.names(current):
                    raise InstallError("Isolated workspace must be empty; existing content is preserved")
                self.stage_name = "provision-" + secrets.token_hex(16)
                self.stage_path = self.view / self.stage_name
                handle, path = self.backend.directory(current, self.stage_name, True)
                self.root = self._keep(handle, path, current, self.stage_name, True, True)
                if self.backend.names(self.root):
                    raise InstallError("New isolated stage is not empty")
            else:
                self.root = current
                self.stage_path = self.view
            self.device = self.root.identity[0]
            self.nodes[""] = self.root
        except BaseException:
            self.close()
            raise

    @property
    def owner_sid(self) -> str:
        """Return the checked Windows owner identity as confinement metadata only."""
        if os.name != "nt":
            raise InstallError("Owner SID requires native Windows isolated storage")
        self.check()
        import win32security
        from chatcopilot.core.windows_storage import _sid
        return win32security.ConvertSidToStringSid(_sid())

    def _keep(self, handle, path, parent, name, directory, private):
        ref = _Ref(handle, path, parent, name, directory, private, ())
        try:
            ref.identity = self.backend.info(ref)[0]
            if parent is not None and self.backend.linked(ref) != ref.identity:
                raise InstallError("Isolated storage changed while opening")
            self.refs.append(ref)
            return ref
        except BaseException:
            self.backend.close(ref)
            raise

    def _check(self, ref):
        identity, stamp, size = self.backend.info(ref)
        if identity != ref.identity or self.backend.linked(ref) != identity:
            raise InstallError("Isolated storage identity changed")
        return stamp, size

    def check(self):
        for ref in self.refs:
            self._check(ref)

    def entry(self, relative: str, *, directory: bool, create: bool):
        # Expected names have already passed the shared archive path policy.
        parts = relative.split("/")
        if not parts or any(not part or part in {".", ".."} or "/" in part or "\\" in part or ":" in part for part in parts):
            raise InstallError("Unsafe isolated storage name")
        parent = self.nodes["/".join(parts[:-1])]
        self._check(parent)
        function = self.backend.directory if directory else self.backend.file
        handle, path = function(parent, parts[-1], create)
        ref = self._keep(handle, path, parent, parts[-1], directory, True)
        if ref.identity[0] != self.device:
            raise InstallError("Isolated storage crossed a filesystem boundary")
        if directory and create and self.backend.names(ref):
            raise InstallError("New isolated directory is not empty")
        self.nodes[relative] = ref
        return ref

    def layout(self, directories: tuple[str, ...], files: tuple[str, ...]):
        expected: dict[str, set[str]] = {path: set() for path in ("", *directories)}
        for path in (*directories, *files):
            parent, _, name = path.rpartition("/")
            expected[parent].add(name)
        for path, names in expected.items():
            if self.backend.names(self.nodes[path]) != names:
                raise InstallError("Isolated tree has missing or unrecorded entries")
        if self.stage_name is not None and self.backend.names(self.workspace) != {self.stage_name}:
            raise InstallError("Isolated workspace changed during staging")
        self.check()

    def close(self):
        error = None
        for ref in reversed(self.refs):
            try:
                self.backend.close(ref)
            except Exception as failure:
                if error is None:
                    error = failure
        self.refs.clear()
        if error is not None:
            raise error
