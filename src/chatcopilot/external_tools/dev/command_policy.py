"""Command selection for trusted shell and delegated validation capabilities."""
from __future__ import annotations

import shlex
from pathlib import Path


_VALIDATION_PREFIXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "python": (("-m", "pytest"), ("-m", "compileall"),
               ("-m", "chatcopilot", "botspec", "validate"),
               ("scripts/check_architecture.py",), ("scripts/check_sdd_specs.py",)),
    "git": tuple((name,) for name in ("diff", "grep", "log", "ls-files", "rev-parse", "show", "status")),
    "npm": tuple(("run", name) for name in ("build", "check", "lint", "test", "typecheck")),
    "cargo": tuple((name,) for name in ("check", "clippy", "test")),
    "go": (("test",), ("vet",)),
    **dict.fromkeys(("ruff", "mypy", "pyright", "rg"), ((),)),
}


def command_argv(command: str, *, validation: bool) -> list[str]:
    if not validation:
        return ["/bin/bash", "--noprofile", "--norc", "-c", command]
    argv = shlex.split(command, posix=True)
    if not argv:
        raise ValueError("validation command is empty")
    executable = Path(argv[0]).name
    if executable in {"python", "python3"}:
        executable = "python"
    tail = tuple(argv[1:])
    prefixes = _VALIDATION_PREFIXES.get(executable, ())
    if not any(tail[:len(prefix)] == prefix for prefix in prefixes):
        raise ValueError("delegated validation accepts tests, builds, linters, repository search and git inspection")
    # Direct argv makes shell punctuation literal. Validators still execute
    # project code; their filesystem authority comes from ExecutionScope.
    return argv
