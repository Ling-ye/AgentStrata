"""Role-aware projection of tool results before they return to a model."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from chatcopilot.contracts.identity import Role
from chatcopilot.contracts.workspace import WorkspaceView


_USER_STRIP_FIELDS = ("console_tail", "doc_links", "details", "stage")


def sanitize_tool_payload(
    payload: Mapping[str, Any],
    *,
    role: Role,
    workspace: WorkspaceView | None = None,
) -> dict[str, Any]:
    """Return a bounded projection; non-owners never receive host path detail."""

    out = dict(payload)
    if role is Role.OWNER:
        return out

    for key in _USER_STRIP_FIELDS:
        out.pop(key, None)

    outputs = out.get("outputs")
    if isinstance(outputs, list):
        out["outputs"] = [
            _workspace_relative_or_basename(item, workspace)
            if isinstance(item, str)
            else item
            for item in outputs
        ]

    summary = out.get("summary")
    if isinstance(summary, str) and summary:
        out["summary"] = _redact_workspace_context(summary, workspace)

    error = out.get("error")
    if isinstance(error, str) and error:
        first_line = error.splitlines()[0] if error.splitlines() else error
        out["error"] = _redact_workspace_context(first_line.strip(), workspace)
    return out


def _workspace_relative_or_basename(
    value: str,
    workspace: WorkspaceView | None,
) -> str:
    if not value:
        return value
    if workspace is None:
        return _basename(value)
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return str(candidate).replace("\\", "/")
        relative = candidate.resolve().relative_to(workspace.root.resolve())
        return str(relative).replace("\\", "/")
    except (OSError, ValueError):
        return _basename(value)


def _basename(value: str) -> str:
    separator = max(value.rfind("/"), value.rfind("\\"))
    return value[separator + 1 :] if separator >= 0 else value


def _redact_workspace_context(
    value: str,
    workspace: WorkspaceView | None,
) -> str:
    redacted = value
    if workspace is not None:
        redacted = redacted.replace(str(workspace.root), "private-space")
    return re.sub(
        r"(?<!\w)(?:workspace|chat|user|name)=[^\s\r\n]+",
        "private-context",
        redacted,
    )


__all__ = ["sanitize_tool_payload"]
