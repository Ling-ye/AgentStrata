"""Native process ownership and job lifetime; imported only on Windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import msvcrt
import socket
import subprocess

import pywintypes
import win32api
import win32con
import win32job
import win32process
import win32security


def identity(pid: int) -> dict | None:
    try:
        handle = win32api.OpenProcess(0x1000, False, pid)
        try:
            if win32process.GetExitCodeProcess(handle) != 259:
                return None
            token = win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY)
            ours = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                if win32security.GetTokenInformation(token, win32security.TokenUser)[0] != win32security.GetTokenInformation(ours, win32security.TokenUser)[0]:
                    return None
            finally:
                token.Close()
                ours.Close()
            created = win32process.GetProcessTimes(handle)["CreationTime"]
            return {"pid": pid, "created": created.isoformat()}
        finally:
            handle.Close()
    except pywintypes.error:
        return None


def owns_listener(pid: int, port: int) -> bool:
    class Row(ctypes.Structure):
        _fields_ = [(field, wintypes.DWORD) for field in
                    ("state", "address", "port", "remote_address", "remote_port", "pid")]
    api = ctypes.WinDLL("iphlpapi")
    size = wintypes.ULONG(0)
    api.GetExtendedTcpTable(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    buffer = ctypes.create_string_buffer(size.value)
    if api.GetExtendedTcpTable(buffer, ctypes.byref(size), False, socket.AF_INET, 5, 0):
        return False
    count = ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buffer) + 4, ctypes.POINTER(Row))
    return any(row.pid == pid and row.state == 2 and socket.ntohs(row.port & 0xffff) == port
               and row.address == int.from_bytes(socket.inet_aton("127.0.0.1"), "little")
               for row in rows[:count])


class ProcessJob:
    """A supervisor-owned job; only explicit external-app launches may break away."""
    def __init__(self):
        self.handle = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] = (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK)
        win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, limits)

    def spawn(self, command, *, cwd, env, output):
        """Assign a suspended child before any application code can run."""
        startup = win32process.STARTUPINFO()
        startup.dwFlags |= win32process.STARTF_USESTDHANDLES
        current = win32api.GetCurrentProcess()
        with open(os.devnull, "rb") as null:
            inherited = [win32api.DuplicateHandle(current, msvcrt.get_osfhandle(file.fileno()),
                current, 0, True, win32con.DUPLICATE_SAME_ACCESS) for file in (null, output)]
        startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited[0], inherited[1], inherited[1]
        process = thread = None
        try:
            process, thread, pid, _ = win32process.CreateProcess(None, subprocess.list2cmdline(command),
                None, None, True, win32process.CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW,
                env, str(cwd), startup)
            win32job.AssignProcessToJobObject(self.handle, process)
            win32process.ResumeThread(thread)
            return OwnedProcess(process, pid)
        except BaseException:
            if process is not None:
                win32process.TerminateProcess(process, 1)
                process.Close()
            raise
        finally:
            if thread is not None:
                thread.Close()
            for handle in inherited:
                handle.Close()

    def close(self):
        self.handle.Close()


class OwnedProcess:
    def __init__(self, handle, pid):
        self.handle, self.pid = handle, pid

    def poll(self):
        import win32event
        if win32event.WaitForSingleObject(self.handle, 0) == win32event.WAIT_TIMEOUT:
            return None
        return win32process.GetExitCodeProcess(self.handle)

    def wait(self, timeout=None):
        import win32event
        milliseconds = win32event.INFINITE if timeout is None else max(0, int(timeout * 1000))
        if win32event.WaitForSingleObject(self.handle, milliseconds) == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired("owned native service", timeout)
        return win32process.GetExitCodeProcess(self.handle)

    def kill(self):
        win32process.TerminateProcess(self.handle, 1)

    def close(self):
        self.handle.Close()


def external_creation_flags() -> int:
    return subprocess.CREATE_BREAKAWAY_FROM_JOB if os.name == "nt" else 0
