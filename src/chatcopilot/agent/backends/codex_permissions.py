"""Project ExecutionScope into Codex's native command permissions."""
from __future__ import annotations

import json
from pathlib import Path

from chatcopilot.contracts.execution_scope import ExecutionScope


def permission_config(
    scope: ExecutionScope | None, *, workdir: Path,
    private_paths: tuple[str, ...], network_access: bool, read_only: bool = False,
) -> tuple[str, ...]:
    # The outer namespace exposes only runtime files and scoped resources.
    # Native children must additionally lose access to the parent's credentials.
    filesystem = {":root": "read", ":slash_tmp": "write"}
    if scope is not None:
        filesystem.update(dict.fromkeys(map(str, scope.readable_roots), "read"))
        if scope.native_write and not read_only:
            filesystem.update(dict.fromkeys(map(str, scope.writable_roots), "write"))
    else:
        filesystem[str(workdir)] = "read"
    filesystem.update(dict.fromkeys(private_paths, "deny"))
    entries = ("permissions.agentstrata.filesystem={" + ", ".join(
        f"{json.dumps(path)}={json.dumps(access)}" for path, access in filesystem.items()
    ) + "}",)
    return (
        'default_permissions="agentstrata"', 'approval_policy="never"',
        *entries,
        f"permissions.agentstrata.network.enabled={str(network_access).lower()}",
        *(("features.network_proxy=true",
           'permissions.agentstrata.network.domains={"*"="allow"}') if network_access else ()),
    )
