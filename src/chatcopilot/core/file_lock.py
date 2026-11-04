"""Advisory file locks with the same interprocess ownership on POSIX and Windows."""
from __future__ import annotations

import os

if os.name != "nt":
    import fcntl
    LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_SH, fcntl.LOCK_UN

    def flock(file, operation):
        return fcntl.flock(file, operation)
else:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.POINTER(_Overlapped)]

    def flock(file, operation: int) -> None:
        descriptor = file if isinstance(file, int) else file.fileno()
        handle = msvcrt.get_osfhandle(descriptor)
        overlapped = _Overlapped()
        if operation & LOCK_UN:
            result = _kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        else:
            flags = (2 if operation & LOCK_EX else 0) | (1 if operation & LOCK_NB else 0)
            result = _kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped))
        if not result:
            code = ctypes.get_last_error()
            if code == 33:
                raise BlockingIOError(11, "file is locked by another process")
            raise ctypes.WinError(code)


__all__ = ["LOCK_SH", "LOCK_EX", "LOCK_NB", "LOCK_UN", "flock"]
