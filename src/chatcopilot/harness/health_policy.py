"""Frozen maintenance policy: protect authority, allow candidate implementations."""
from __future__ import annotations

import ast
import os
from pathlib import Path

from chatcopilot.core.source_manifest import is_private_environment_path
from chatcopilot.harness.models import HarnessError

POLICY_PREFIXES = ("tests", "specs", ".github", ".cursor", "requirements", "docs/reference", "src/chatcopilot/evals/suites")
POLICY_NAMES = frozenset({"AGENTS.md", "SECURITY.md", "CODE_OF_CONDUCT.md", ".gitignore", ".gitattributes", "pyproject.toml",
                          "uv.lock", "package.json", "package-lock.json", "pytest.ini",
                          "conftest.py", "tox.ini", "setup.cfg", "mypy.ini", "ruff.toml", ".ruff.toml",
                          "tsconfig.json", "vitest.config.ts", "rsbuild.config.ts"})

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


def requires_regression(name: str) -> bool:
    return checker_path(name) or name.startswith(("src/chatcopilot/harness/", "src/chatcopilot/authorization/")) or name in {
        "src/chatcopilot/core/source_snapshot.py", "src/chatcopilot/core/source_manifest.py",
        "src/chatcopilot/core/scoped_process.py", "src/chatcopilot/agent/backends/codex_permissions.py",
    }


def public_example(name: str) -> bool:
    return Path(name).name == ".env.example" or name.endswith(".env.example") or Path(name).name == "env.example"


def documentation_file(name: str) -> bool:
    """Read scope for maintainer prose, excluding executable prompts and licensed data."""
    path = Path(name)
    if any(part in {"prompts", "skills", "fixtures", "vendor"} for part in path.parts):
        return False
    return (path.suffix == ".md" and (name.startswith(("docs/", "specs/"))
            or name in {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md", "CHANGELOG.md", "CODE_OF_CONDUCT.md", ".github/pull_request_template.md"}
            or (path.name == "README.md" and path.parts[0] in {"src", "bots", "deploy", "console"}))
            or name.startswith(".cursor/rules/") and path.suffix == ".mdc")


def scan_path(name: str, scope: str) -> bool:
    return scope_path(name, scope) or (scope in {"all", "docs"} and documentation_file(name))


def scope_path(name: str, scope: str) -> bool:
    roots = {"all": ("src", "console", "scripts", "docs"), "runtime": ("src",),
             "console": ("console",), "docs": ("docs",)}
    return (any(name == p or name.startswith(p + "/") for p in roots[scope])
            or (scope == "all" and public_example(name))
            or (scope in {"all", "docs"} and documentation_file(name) and not policy_path(name)))


def writable_paths(root: Path, scope: str) -> tuple[Path, ...]:
    roots = [root / name for name in ("src", "console", "scripts", "docs")
             if scope_path(name, scope)]
    if scope == "all":
        roots.extend((root / name).parent for name in source_files(root) if public_example(name))
    if scope in {"all", "docs"}:
        roots.extend(root / name for name in source_files(root)
                     if documentation_file(name) and not policy_path(name)
                     and not any((root / name).is_relative_to(parent) for parent in roots))
    return tuple(dict.fromkeys(p for p in roots if p.exists()))


def protected_paths(root: Path) -> tuple[Path, ...]:
    roots = [root / name for name in (".git", *POLICY_PREFIXES) if (root / name).exists()]
    for name in source_files(root):
        path = root / name
        if policy_path(name) and not any(path == parent or path.is_relative_to(parent) for parent in roots):
            roots.append(path)
    return tuple(roots)


def declarations(content: bytes, names: set[str]) -> dict[str, str]:
    result = {}
    for node in ast.parse(content).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in names:
                    result[target.id] = ast.dump(node.value, include_attributes=False)
    return result


_ARTIFACT_DIRS = {".git", ".cache", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
                  ".ruff_cache", ".mypy_cache", "build", "dist"}


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
