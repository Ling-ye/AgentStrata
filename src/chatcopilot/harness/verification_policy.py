"""Frozen maintenance policy: protect authority, allow candidate implementations."""
from __future__ import annotations

import os
from pathlib import Path

from chatcopilot.core.source_manifest import is_private_environment_path
from chatcopilot.harness.models import HarnessError

POLICY_PREFIXES = ("tests", "specs", ".github", ".cursor", "requirements", "docs/reference", "src/chatcopilot/evals/suites")
POLICY_NAMES = frozenset({"AGENTS.md", "SECURITY.md", "CODE_OF_CONDUCT.md", ".gitignore", ".gitattributes", "pyproject.toml",
                          "uv.lock", "package.json", "package-lock.json", "pytest.ini",
                          "conftest.py", "tox.ini", "setup.cfg", "mypy.ini", "ruff.toml", ".ruff.toml",
                          "tsconfig.json", "vitest.config.ts", "rsbuild.config.ts"})

GOVERNANCE_AUTHORITY = frozenset({".gitleaks.toml", "src/chatcopilot/contracts/execution_scope.py",
    "src/chatcopilot/authorization/policy.py", "src/chatcopilot/harness/verification_policy.py",
    "scripts/verify_release_artifacts.py"})


def governance_policy_path(name: str) -> bool:
    return (policy_path(name) or name in GOVERNANCE_AUTHORITY or name.startswith("scripts/check_")
            or name.startswith("src/chatcopilot/component_catalog/audit/"))

CHECKER_PREFIXES = ("scripts/", "src/chatcopilot/component_catalog/audit")
CHECKER_FILES = {"src/chatcopilot/component_catalog/__init__.py"}


def checker_path(name: str) -> bool:
    return name in CHECKER_FILES or name.startswith(CHECKER_PREFIXES)


def policy_path(name: str) -> bool:
    path = Path(name)
    test_file = any(path.name.endswith(suffix) for suffix in (".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx")) or "__tests__" in path.parts
    return (test_file or path.name in POLICY_NAMES or any(name == p or name.startswith(p + "/") for p in POLICY_PREFIXES)
            or name in {"docs/maintenance.md", "src/chatcopilot/evals/business_policy.py"} or (name.startswith("docs/") and path.name.startswith("ai-"))
            or path.suffix in {".rules"} or is_private_environment_path(name))


_ARTIFACT_DIRS = {".git", ".cache", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "build", "dist"}

def source_files(root: Path) -> list[str]:
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                raise HarnessError("unsafe_candidate", "候选目录含符号链接")
        dirs[:] = sorted(name for name in dirs if name not in _ARTIFACT_DIRS and not name.endswith(".egg-info"))
        names.extend((Path(directory) / name).relative_to(root).as_posix() for name in sorted(files)
                     if Path(directory) != root or name != ".git")
    return names
