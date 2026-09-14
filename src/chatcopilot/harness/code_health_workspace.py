"""Candidate edits are measured against a frozen working tree, never against HEAD."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from chatcopilot.core.source_snapshot import manifest_digest, source_manifest, verify_copy
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.code_health_rules import in_scope
from chatcopilot.harness.workspace import writable_paths as product_paths

_PROTECTED = (
    "src/chatcopilot/harness", "src/chatcopilot/evals", "src/chatcopilot/authorization",
    "src/chatcopilot/agent/backends/codex_permissions.py", "src/chatcopilot/agent/context/prompt_plan.py",
    "src/chatcopilot/external_tools/codex_cli", "src/chatcopilot/core/scoped_process.py",
    "src/chatcopilot/core/private_sqlite.py", "src/chatcopilot/core/file_integrity.py",
    "src/chatcopilot/core/source_snapshot.py", "src/chatcopilot/core/source_manifest.py",
    "src/chatcopilot/core/candidate_configuration.py", "src/chatcopilot/core/inspection.py",
    "src/chatcopilot/core/observability_redaction.py", "src/chatcopilot/core/trace_archive.py",
    "src/chatcopilot/core/trace_capture.py", "docs/sdd.md",
    "console/web/src/features/codeHealth", "console/web/src/pages/CodeHealthPage.tsx",
)


def writable_paths(root: Path, scope: str) -> tuple[Path, ...]:
    roots = (*product_paths(root), root / "docs", root / "console/web/src")
    protected = protected_paths(root)
    return tuple(path for path in roots if path.is_dir() and in_scope(path.relative_to(root).as_posix(), scope)
                 and not any(path == parent or path.is_relative_to(parent) for parent in protected))


def protected_paths(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in (*(root / name for name in _PROTECTED),
                                    *(root / "docs").glob("ai-*.md"),
                                    *(root / "src").rglob("AGENTS.md"),
                                    *(root / "docs").rglob("AGENTS.md"),
                                    *(root / "console/web/src").rglob("AGENTS.md")) if path.exists())


def permitted(path: str, root: Path, scope: str) -> bool:
    target = root / path
    return (in_scope(path, scope)
            and any(target.is_relative_to(parent) for parent in writable_paths(root, scope))
            and not any(target == parent or target.is_relative_to(parent)
                        for parent in protected_paths(root))
            and Path(path).name != "AGENTS.md")


def changes(root: Path, baseline: dict[str, Any], scope: str) -> list[str]:
    current = source_manifest(root)
    names = sorted(name for name in baseline.keys() | current.keys()
                   if baseline.get(name) != current.get(name))
    if any(not permitted(name, root, scope) for name in names):
        raise HarnessError("protected_change", "候选修改了范围外文件或受保护的验收、权限及执行控制")
    return names


def restore(root: Path, frozen: Path, baseline: dict[str, Any], *, expected_digest: str | None = None) -> None:
    verify_copy(frozen, baseline)
    current = source_manifest(root)
    if expected_digest is not None and manifest_digest(current) != expected_digest:
        raise HarnessError("workspace_changed", "候选在验收后被外部修改，停止恢复")
    for name in sorted(current.keys() | baseline.keys()):
        if current.get(name) == baseline.get(name):
            continue
        target = root / name
        if target.resolve() != target:
            raise HarnessError("workspace_changed", "候选路径不再安全")
        if name not in baseline:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (frozen / name).read_bytes()
        fd = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        target.chmod(0o700 if baseline[name]["executable"] else 0o600)
    if manifest_digest(source_manifest(root)) != manifest_digest(baseline):
        raise HarnessError("workspace_changed", "候选与冻结源码不一致")


def save_patch(root: Path, frozen: Path, names: list[str], output: Path) -> str:
    """Use Git's binary patch encoding without staging or creating a baseline commit."""
    chunks = []
    for name in names:
        before, after = frozen / name, root / name
        result = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--no-ext-diff", "--no-textconv", "--",
             str(before) if before.exists() else "/dev/null",
             str(after) if after.exists() else "/dev/null"],
            capture_output=True, timeout=30, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        if result.returncode not in {0, 1}:
            raise HarnessError("patch_failed", "无法导出工作区快照补丁")
        content = result.stdout
        for prefix in (str(frozen).lstrip("/"), str(root).lstrip("/")):
            content = content.replace(("a/" + prefix + "/").encode(), b"a/")
            content = content.replace(("b/" + prefix + "/").encode(), b"b/")
        chunks.append(content)
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    content = b"".join(chunks)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    return hashlib.sha256(content).hexdigest()
