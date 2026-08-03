#!/usr/bin/env python3
"""AST-based architecture boundary checks for AgentStrata."""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "chatcopilot"


@dataclass(frozen=True)
class Rule:
    name: str
    root: Path
    forbidden: tuple[str, ...]
    allowed: tuple[tuple[str, str], ...] = ()


RULES = (
    Rule(
        name="contracts_is_pure",
        root=SRC / "contracts",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.botspec",
            "chatcopilot.external_tools",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
        ),
    ),
    Rule(
        name="core_no_upper_layers",
        root=SRC / "core",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.botspec",
            "chatcopilot.external_tools",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
        ),
    ),
    Rule(
        name="agent_no_upper_layers",
        root=SRC / "agent",
        forbidden=("chatcopilot.middleware", "chatcopilot.platforms"),
    ),
    Rule(
        name="agent_no_botspec",
        root=SRC / "agent",
        forbidden=("chatcopilot.botspec",),
    ),
    Rule(
        name="platforms_no_agent_or_middleware",
        root=SRC / "platforms",
        forbidden=("chatcopilot.agent", "chatcopilot.middleware"),
    ),
    Rule(
        name="botspec_no_agent",
        root=SRC / "botspec",
        forbidden=("chatcopilot.agent",),
    ),
    Rule(
        name="external_tools_no_upper_layers",
        root=SRC / "external_tools",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.botspec",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
        ),
    ),
    Rule(
        name="middleware_no_concrete_platform_modules",
        root=SRC / "middleware",
        forbidden=("chatcopilot.platforms.feishu", "chatcopilot.platforms.qq"),
    ),
    Rule(
        name="console_no_agent_or_botspec_internals",
        root=ROOT / "console",
        forbidden=(
            "chatcopilot.agent.subagents",
            "chatcopilot.botspec.registry",
        ),
    ),
)


def _python_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module)
    return found


def check_rules() -> dict[str, dict[str, list[str]]]:
    violations: dict[str, dict[str, list[str]]] = {}
    for rule in RULES:
        allowed = set(rule.allowed)
        rule_violations: dict[str, list[str]] = {}
        for path in _python_files(rule.root):
            rel = str(path.relative_to(ROOT))
            bad = []
            for module in _imports(path):
                if not _matches(module, rule.forbidden):
                    continue
                if (rel, module) in allowed:
                    continue
                bad.append(module)
            if bad:
                rule_violations[rel] = sorted(set(bad))
        if rule_violations:
            violations[rule.name] = rule_violations
    return violations


def _semantic_invariants() -> dict[str, dict[str, list[str]]]:
    """Check cross-file invariants that import-prefix rules cannot express."""
    violations: dict[str, dict[str, list[str]]] = {}

    turn_path = SRC / "agent" / "turn.py"
    if turn_path.exists():
        imports = _imports(turn_path)
        if "chatcopilot.agent.session" in imports:
            violations["session_turn_no_private_cycle"] = {
                str(turn_path.relative_to(ROOT)): ["chatcopilot.agent.session"]
            }

    server_path = SRC / "middleware" / "acp" / "server.py"
    if server_path.exists():
        source = server_path.read_text(encoding="utf-8-sig")
        forbidden = tuple(
            name
            for name in ("route_orchestrator", "run_code_route", "TurnRouteDetector")
            if name in source
        )
        if forbidden:
            violations["acp_no_cross_backend_routing"] = {
                str(server_path.relative_to(ROOT)): list(forbidden)
            }
        if "already_completed" in source:
            violations["acp_pipeline_has_no_noop_completion_flag"] = {
                str(server_path.relative_to(ROOT)): ["already_completed"]
            }

    gateway_path = SRC / "middleware" / "mcp" / "session_gateway.py"
    if gateway_path.exists():
        gateway_source = gateway_path.read_text(encoding="utf-8-sig")
        if "discover_tools" in gateway_source:
            violations["codex_gateway_uses_exact_session_tools"] = {
                str(gateway_path.relative_to(ROOT)): ["discover_tools"]
            }

    extracted_boundaries = (
        (
            ROOT / "console" / "control" / "operations.py",
            {"follow_log", "follow_console_log"},
            "console_operations_has_no_observability_implementation",
        ),
    )
    for path, forbidden_names, rule_name in extracted_boundaries:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        duplicate = sorted(defined & forbidden_names)
        if duplicate:
            violations[rule_name] = {
                str(path.relative_to(ROOT)): duplicate
            }

    for path in (
        SRC / "external_tools" / "codebase" / "changes.py",
        SRC / "external_tools" / "repository_tasks" / "service.py",
    ):
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        forbidden_git_calls: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if function_name not in {"_git", "run_git"}:
                continue
            literals = {
                arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            }
            forbidden_git_calls.extend(sorted(literals & {"commit", "push"}))
        if forbidden_git_calls:
            violations.setdefault("repository_tasks_no_git_commit_or_push", {})[
                str(path.relative_to(ROOT))
            ] = sorted(set(forbidden_git_calls))

    removed_sources = (
        SRC / "middleware" / "acp" / "code_route.py",
        SRC / "middleware" / "acp" / "route_orchestrator.py",
    )
    present = [str(path.relative_to(ROOT)) for path in removed_sources if path.exists()]
    if present:
        violations["removed_legacy_sources_do_not_return"] = {
            "repository": sorted(present)
        }

    private_surfaces = {"chatcopilot.external_tools.mcp_admin.tools"}
    allowed_private_importers: set[str] = set()
    private_violations: dict[str, list[str]] = {}
    for path in _python_files(SRC):
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in allowed_private_importers:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        bad: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module not in private_surfaces:
                continue
            bad.extend(alias.name for alias in node.names if alias.name.startswith("_"))
        if bad:
            private_violations[rel] = sorted(set(bad))
    if private_violations:
        violations["no_private_cross_domain_imports"] = private_violations
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    violations = check_rules()
    violations.update(_semantic_invariants())
    if not violations:
        print("OK: architecture boundaries")
        return 0
    for rule, files in violations.items():
        print(f"{rule}:")
        for file, imports in files.items():
            print(f"  {file}: {', '.join(imports)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
