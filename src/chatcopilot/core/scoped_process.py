"""Shared OS confinement for commands operating on host-bound resources."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

from chatcopilot.contracts.execution_scope import ExecutionScope


_SYSTEM_READS = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/ld.so.cache",
    "/etc/localtime",
)


def _parents(paths: tuple[Path, ...]) -> list[str]:
    parents = {p for path in paths for p in path.parents if str(p) != "/"}
    return [
        part
        for p in sorted(parents, key=lambda x: (len(x.parts), str(x)))
        for part in ("--dir", str(p))
    ]


def scope_mounts(scope: ExecutionScope) -> list[str]:
    roots = tuple(dict.fromkeys((*scope.readable_roots, *scope.writable_roots)))
    for root in roots:
        if (
            not root.is_absolute()
            or root.resolve() != root
            or not root.is_dir()
            or root.is_symlink()
        ):
            raise ValueError("execution scope requires canonical existing directories")
    args = _parents(roots)
    for root in sorted(roots, key=lambda p: len(p.parts)):
        args.extend(
            ("--bind" if root in scope.writable_roots else "--ro-bind", str(root), str(root))
        )
    for root in scope.protected_roots:
        if root.exists():
            args.extend(("--ro-bind", str(root), str(root)))
    for root in scope.hidden_roots:
        if root.is_dir():
            args.extend(("--tmpfs", str(root)))
        elif root.exists():
            args.extend(("--ro-bind", "/dev/null", str(root)))
    return args


def require_bubblewrap() -> str:
    executable = shutil.which("bwrap")
    if not executable:
        raise RuntimeError(
            "bubblewrap (bwrap) is required for isolated execution; "
            "install bubblewrap on the Linux/WSL host and retry"
        )
    return str(Path(executable).resolve())


def sandbox_command(command: list[str], *, scope: ExecutionScope, cwd: Path) -> list[str]:
    if not scope.permits(cwd):
        raise ValueError("command cwd is outside its execution scope")
    bwrap = require_bubblewrap()
    args = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--dir",
        "/sandbox-home",
    ]
    for value in _SYSTEM_READS:
        if Path(value).exists():
            args.extend(("--ro-bind", value, value))
    runtime_paths = tuple(
        dict.fromkeys((Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()))
    )
    args.extend(_parents(runtime_paths))
    for p in runtime_paths:
        if p.exists() and not str(p).startswith("/usr/"):
            args.extend(("--ro-bind", str(p), str(p)))
    args.extend(scope_mounts(scope))
    args.extend(
        (
            "--setenv",
            "HOME",
            "/sandbox-home",
            "--setenv",
            "PATH",
            f"{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "GIT_OPTIONAL_LOCKS",
            "0",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--chdir",
            str(cwd),
            "--",
        )
    )
    return [*args, *command]
