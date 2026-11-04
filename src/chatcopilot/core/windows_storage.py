"""Windows handles and ACLs for the existing private-storage contract.

Directory handles exclude delete sharing, so anchored relative operations cannot
be redirected by renaming their parent. Private metadata expresses a checked SID
and DACL as the storage contract's owner/mode fields; NTFS chmod bits alone never
establish privacy. This module is imported only on Windows.
"""
from __future__ import annotations

import errno
from contextlib import contextmanager
import msvcrt
import os
import re
from pathlib import Path
import stat

import pywintypes
import win32api
import win32con
import win32file
import win32security

DIRECTORY = 0x40000000
NOFOLLOW = 0x20000000


def api_path(path) -> str:
    """Use NT's local-drive namespace without requiring a system registry change."""
    value = str(Path(path).absolute()).removeprefix("\\\\?\\")
    if not re.match(r"^[A-Za-z]:[\\/]", value):
        raise PermissionError("private storage requires a local Windows drive")
    return "\\\\?\\" + value


@contextmanager
def _held(handle):
    try:
        yield handle
    finally:
        handle.Close()


def _sid():
    with _held(win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)) as token:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]


def security_attributes():
    sid = win32security.ConvertSidToStringSid(_sid())
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", 1)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.bInheritHandle = False
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _error(error):
    code = error.winerror
    if code in (2, 3):
        return FileNotFoundError(errno.ENOENT, "storage entry is missing")
    if code in (80, 183):
        return FileExistsError(errno.EEXIST, "storage entry exists")
    return OSError(errno.EACCES, f"Windows storage operation failed ({code})")


def reject_reparse(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(api_path(current))
        except FileNotFoundError:
            break
        if info.st_file_attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PermissionError("storage path contains a reparse point")


def _handle(path, access=win32con.READ_CONTROL, *, creation=win32con.OPEN_EXISTING,
            private=False, share_delete=False):
    path = Path(path).absolute()
    reject_reparse(path)
    try:
        return win32file.CreateFile(api_path(path), access,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | (win32con.FILE_SHARE_DELETE if share_delete else 0),
            security_attributes() if private else None,
            creation, win32con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000, None)
    except pywintypes.error as error:
        raise _error(error) from None


def _validate(handle, *, private=True, single_link=False) -> bool:
    information = win32file.GetFileInformationByHandle(handle)
    attributes, links = information[0], information[7]
    if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError("storage entry is a reparse point")
    directory = bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY)
    if single_link and not directory and links != 1:
        raise PermissionError("private storage file must have exactly one link")
    if private:
        descriptor = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
        owner = descriptor.GetSecurityDescriptorOwner()
        sid = _sid()
        if owner != sid:
            raise PermissionError("private storage belongs to another Windows user")
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            raise PermissionError("private storage requires an explicit DACL")
        # Python 3.13 mkdir(mode=0700) uses OWNER RIGHTS. It is safe only
        # after checking that the object's actual owner is the current SID.
        allowed = {win32security.ConvertSidToStringSid(sid), "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
        for index in range(acl.GetAceCount()):
            ace = acl.GetAce(index)
            kind, flags = ace[0]
            if flags & win32con.INHERIT_ONLY_ACE:
                continue
            if kind == win32security.ACCESS_DENIED_ACE_TYPE:
                continue
            if kind != win32security.ACCESS_ALLOWED_ACE_TYPE or (
                    ace[1] and win32security.ConvertSidToStringSid(ace[-1]) not in allowed):
                raise PermissionError("private storage grants access to another Windows account")
    return directory


def tighten(path: Path) -> None:
    with _held(_handle(path, win32con.READ_CONTROL | win32con.WRITE_DAC)) as handle:
        _validate(handle, private=False, single_link=True)
        descriptor = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT,
                                                   win32security.OWNER_SECURITY_INFORMATION)
        if descriptor.GetSecurityDescriptorOwner() != _sid():
            raise PermissionError("refusing to change another user's storage")
        acl = security_attributes().SECURITY_DESCRIPTOR.GetSecurityDescriptorDacl()
        win32security.SetSecurityInfo(handle, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        _validate(handle)


def path_for_fd(descriptor: int) -> Path:
    raw = win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(descriptor), 0)
    if raw.startswith("\\\\?\\UNC\\"):
        raise PermissionError("private application storage must be on a native Windows drive")
    return Path(raw.removeprefix("\\\\?\\"))


def relative(path, dir_fd=None) -> Path:
    if dir_fd is None:
        return Path(path).absolute()
    value = Path(path)
    if value.is_absolute() or len(value.parts) != 1 or value.name in {".", ".."} or ":" in str(value):
        raise PermissionError("anchored storage operation requires one path segment")
    return path_for_fd(dir_fd) / value


def open_file(path, flags, mode=0o777, *, dir_fd=None):
    path = relative(path, dir_fd)
    directory = bool(flags & DIRECTORY)
    creation = win32con.OPEN_EXISTING
    if flags & os.O_CREAT:
        creation = win32con.CREATE_NEW if flags & os.O_EXCL else win32con.OPEN_ALWAYS
    access = win32con.GENERIC_READ
    if flags & (os.O_WRONLY | os.O_RDWR):
        access |= win32con.GENERIC_WRITE
    handle = _handle(path, access | win32con.READ_CONTROL,
                     creation=creation, private=bool(flags & os.O_CREAT), share_delete=not directory)
    try:
        actual_directory = _validate(handle, private=not directory, single_link=not directory)
        if directory and not actual_directory:
            raise NotADirectoryError(str(path))
        if flags & os.O_TRUNC:
            win32file.SetEndOfFile(handle)
        descriptor = msvcrt.open_osfhandle(handle.Detach(), (flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)) | os.O_BINARY)
        return descriptor
    finally:
        handle.Close()


def metadata(path=None, *, descriptor=None):
    if descriptor is None:
        with _held(_handle(path)) as handle:
            _validate(handle)
            info = os.lstat(api_path(path))
    else:
        _validate(msvcrt.get_osfhandle(descriptor))
        info = os.fstat(descriptor)
    values = list(info)
    values[0] = stat.S_IFMT(info.st_mode) | (0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
    values[4] = 0  # A successfully checked current-user SID, not the CRT's synthetic UID.
    return os.stat_result(values, {"st_atime_ns": info.st_atime_ns, "st_mtime_ns": info.st_mtime_ns,
                                   "st_ctime_ns": info.st_ctime_ns})


def mkdir(path, mode=0o777, *, dir_fd=None):
    path = relative(path, dir_fd)
    reject_reparse(path)
    try:
        win32file.CreateDirectory(api_path(path), security_attributes())
    except pywintypes.error as error:
        raise _error(error) from None


def sync(descriptor):
    # Windows has no POSIX directory fsync. File buffers are flushed before
    # replacement; MoveFileEx's WRITE_THROUGH persists the namespace change.
    if not win32file.GetFileInformationByHandle(msvcrt.get_osfhandle(descriptor))[0] & win32con.FILE_ATTRIBUTE_DIRECTORY:
        os.fsync(descriptor)


def replace(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
    source, destination = relative(source, src_dir_fd), relative(destination, dst_dir_fd)
    reject_reparse(source)
    reject_reparse(destination)
    try:
        win32file.MoveFileEx(api_path(source), api_path(destination),
                            win32con.MOVEFILE_REPLACE_EXISTING | 0x00000008)  # MOVEFILE_WRITE_THROUGH
    except pywintypes.error as error:
        raise _error(error) from None
