"""Compile one caller's configured resource view for every execution backend."""

from pathlib import Path

from chatcopilot.contracts.execution_scope import ExecutionScope
from chatcopilot.contracts.identity import role_value


def execution_scope(
    role: object, workspace: Path, project_roots: tuple[Path, ...] = ()
) -> ExecutionScope:
    owner = role_value(role) == "owner"
    projects = tuple(dict.fromkeys(path.resolve() for path in project_roots)) if owner else ()
    roots = tuple(dict.fromkeys((workspace.resolve(), *projects)))
    protected: list[Path] = []
    hidden: list[Path] = []
    for root in roots:
        for name in (".git", ".conversation-state", ".backend-sessions"):
            path = root / name
            (protected if owner and name == ".git" else hidden).append(path)
    hidden.extend(
        workspace.resolve() / name
        for name in ("jobs", "tasks", "transcripts", "IDENTITY.json", "MEMORY.md", "PERSONA.md")
    )
    return ExecutionScope(
        roots,
        roots if owner else (workspace.resolve(),),
        projects,
        tuple(protected),
        tuple(hidden),
        native_write=owner,
    )
