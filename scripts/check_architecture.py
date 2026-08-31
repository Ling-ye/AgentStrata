#!/usr/bin/env python3
"""Validate AgentStrata's declared dependency DAG and module import graph."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "chatcopilot"


@dataclass(frozen=True)
class Rule:
    name: str
    root: Path
    forbidden: tuple[str, ...]
    allowed: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ModuleFile:
    name: str
    path: Path
    area: str
    is_package: bool = False


@dataclass(frozen=True)
class ImportReference:
    source: str
    imported: str
    target: str | None


RULES = (
    Rule(
        name="contracts_is_pure",
        root=SRC / "contracts",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.application",
            "chatcopilot.authorization",
            "chatcopilot.botspec",
            "chatcopilot.channels",
            "chatcopilot.external_tools",
            "chatcopilot.gateway",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
    ),
    Rule(
        name="core_no_upper_layers",
        root=SRC / "core",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.application",
            "chatcopilot.authorization",
            "chatcopilot.botspec",
            "chatcopilot.channels",
            "chatcopilot.external_tools",
            "chatcopilot.gateway",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
    ),
    Rule(
        name="agent_no_upper_layers",
        root=SRC / "agent",
        forbidden=(
            "chatcopilot.application",
            "chatcopilot.authorization",
            "chatcopilot.botspec",
            "chatcopilot.channels",
            "chatcopilot.gateway",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
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
        name="authorization_no_runtime_or_protocol_layers",
        root=SRC / "authorization",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.application",
            "chatcopilot.botspec",
            "chatcopilot.channels",
            "chatcopilot.external_tools",
            "chatcopilot.gateway",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
    ),
    Rule(
        name="channels_do_not_own_domain_authority",
        root=SRC / "channels",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.application",
            "chatcopilot.authorization",
            "chatcopilot.botspec",
            "chatcopilot.external_tools",
            "chatcopilot.gateway",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
    ),
    Rule(
        name="application_has_no_protocol_or_transport_implementation",
        root=SRC / "application",
        forbidden=(
            "chatcopilot.channels",
            "chatcopilot.gateway",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
    ),
    Rule(
        name="gateway_composes_application_not_legacy_runtime",
        root=SRC / "gateway",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.external_tools",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
            "chatcopilot.protocols",
        ),
    ),
    Rule(
        name="acp_protocol_is_only_a_gateway_edge",
        root=SRC / "protocols" / "acp",
        forbidden=(
            "chatcopilot.agent",
            "chatcopilot.application",
            "chatcopilot.authorization",
            "chatcopilot.botspec",
            "chatcopilot.channels",
            "chatcopilot.external_tools",
            "chatcopilot.middleware",
            "chatcopilot.platforms",
        ),
    ),
    Rule(
        name="console_no_agent_or_botspec_internals",
        root=ROOT / "console",
        forbidden=("chatcopilot.agent.subagents", "chatcopilot.botspec.registry"),
    ),
)


# Same-area imports are always allowed. Cross-area imports must be listed here;
# this declaration is itself checked for cycles before source edges are checked.
AREA_DEPENDENCIES: Mapping[str, frozenset[str]] = {
    "contracts": frozenset(),
    "project": frozenset(),
    "core": frozenset({"contracts", "project"}),
    "authorization": frozenset({"contracts", "core", "project"}),
    "channels": frozenset({"contracts", "core", "project"}),
    "tool_packs": frozenset({"contracts"}),
    "component_catalog": frozenset({"contracts", "core", "tool_packs"}),
    "external_tools": frozenset({"contracts", "core", "project"}),
    "platforms": frozenset({"contracts", "core", "project"}),
    "agent": frozenset(
        {
            "contracts",
            "core",
            "component_catalog",
            "external_tools",
            "project",
            "tool_packs",
        }
    ),
    "botspec": frozenset(
        {
            "component_catalog",
            "contracts",
            "core",
            "external_tools",
            "platforms",
            "project",
            "tool_packs",
        }
    ),
    "application": frozenset(
        {
            "agent",
            "authorization",
            "botspec",
            "contracts",
            "core",
            "tool_packs",
        }
    ),
    "gateway": frozenset(
        {
            "application",
            "authorization",
            "botspec",
            "channels",
            "contracts",
            "core",
            "project",
        }
    ),
    "protocols": frozenset({"contracts", "core", "gateway", "project"}),
    "middleware": frozenset(
        {
            "agent",
            "application",
            "authorization",
            "botspec",
            "component_catalog",
            "contracts",
            "core",
            "external_tools",
            "platforms",
            "project",
            "protocols",
            "tool_packs",
        }
    ),
    "evals": frozenset(
        {
            "agent",
            "application",
            "authorization",
            "botspec",
            "channels",
            "component_catalog",
            "contracts",
            "core",
            "external_tools",
            "gateway",
            "middleware",
            "platforms",
            "project",
            "protocols",
            "tool_packs",
        }
    ),
    "entrypoints": frozenset(
        {
            "agent",
            "application",
            "authorization",
            "botspec",
            "channels",
            "component_catalog",
            "contracts",
            "core",
            "evals",
            "external_tools",
            "gateway",
            "middleware",
            "platforms",
            "project",
            "protocols",
            "tool_packs",
        }
    ),
}

AREA_IMPORT_EXCEPTIONS = frozenset(
    {("src/chatcopilot/agent/search/probe.py", "chatcopilot.search_probe")}
)

COMPATIBILITY_IMPORTS = (
    "chatcopilot.agent.config",
    "chatcopilot.agent.concurrency",
    "chatcopilot.agent.llm_client",
    "chatcopilot.agent.protocol",
    "chatcopilot.agent.research",
    "chatcopilot.botspec.mcp_catalog",
    "chatcopilot.core.workspace",
    "chatcopilot.external_tools.shared.tool_spec",
    "chatcopilot.middleware.runtime.workspace",
)

def _python_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _matches(module: str, prefixes: Sequence[str]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _module_name(path: Path, source_root: Path, prefix: str) -> tuple[str, bool]:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    suffix = ".".join(parts)
    return prefix + ("." + suffix if suffix else ""), is_package


def _module_area(module: str) -> str:
    if module == "chatcopilot.project":
        return "project"
    if module.startswith("console"):
        return "entrypoints"
    if not module.startswith("chatcopilot."):
        return "entrypoints"
    top = module.split(".", 2)[1]
    if top in AREA_DEPENDENCIES and top != "entrypoints":
        return top
    return "entrypoints"


def _production_modules() -> dict[str, ModuleFile]:
    modules: dict[str, ModuleFile] = {}
    for source_root, prefix in ((SRC, "chatcopilot"), (ROOT / "console", "console")):
        for path in _python_files(source_root):
            name, is_package = _module_name(path, source_root, prefix)
            modules[name] = ModuleFile(
                name=name,
                path=path,
                area=_module_area(name),
                is_package=is_package,
            )
    return modules


def _absolute_import_base(record: ModuleFile, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = record.name if record.is_package else record.name.rpartition(".")[0]
    parts = package.split(".") if package else []
    keep = len(parts) - node.level + 1
    if keep < 0:
        return ""
    anchor = ".".join(parts[:keep])
    if node.module:
        return anchor + ("." if anchor else "") + node.module
    return anchor


def _resolve_internal_module(candidate: str, modules: Mapping[str, ModuleFile]) -> str | None:
    current = candidate
    while current:
        if current in modules:
            return current
        current = current.rpartition(".")[0]
    return None


def _import_references(
    record: ModuleFile,
    modules: Mapping[str, ModuleFile],
) -> tuple[ImportReference, ...]:
    tree = ast.parse(record.path.read_text(encoding="utf-8-sig"), filename=str(record.path))
    references: list[ImportReference] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                references.append(
                    ImportReference(
                        source=record.name,
                        imported=alias.name,
                        target=_resolve_internal_module(alias.name, modules),
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_import_base(record, node)
            if not base:
                continue
            base_target = _resolve_internal_module(base, modules)
            for alias in node.names:
                candidate = base + "." + alias.name if alias.name != "*" else base
                target = candidate if candidate in modules else base_target
                references.append(
                    ImportReference(
                        source=record.name,
                        imported=candidate if candidate in modules else base,
                        target=target,
                    )
                )
    return tuple(references)


def _private_cross_area_imports(
    record: ModuleFile,
    modules: Mapping[str, ModuleFile],
) -> tuple[str, ...]:
    def is_private_segment(value: str) -> bool:
        return value.startswith("_") and not (
            value.startswith("__") and value.endswith("__")
        )

    def crosses_area(candidate: str) -> bool:
        target = _resolve_internal_module(candidate, modules)
        return target is not None and modules[target].area != record.area

    tree = ast.parse(record.path.read_text(encoding="utf-8-sig"), filename=str(record.path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if crosses_area(alias.name) and any(
                    is_private_segment(part) for part in alias.name.split(".")
                ):
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_import_base(record, node)
            if not base or not crosses_area(base):
                continue
            if any(is_private_segment(part) for part in base.split(".")):
                bad.append(base)
            bad.extend(
                f"{base}:{alias.name}"
                for alias in node.names
                if is_private_segment(alias.name)
            )
    return tuple(sorted(set(bad)))


def _imports(
    path: Path,
    modules: Mapping[str, ModuleFile] | None = None,
) -> list[str]:
    if path.is_relative_to(SRC):
        name, is_package = _module_name(path, SRC, "chatcopilot")
    else:
        name, is_package = _module_name(path, ROOT / "console", "console")
    record = ModuleFile(name=name, path=path, area=_module_area(name), is_package=is_package)
    production_modules = modules if modules is not None else _production_modules()
    return [item.imported for item in _import_references(record, production_modules)]


def _merge(
    destination: dict[str, dict[str, list[str]]],
    source: Mapping[str, Mapping[str, Sequence[str]]],
) -> None:
    for rule, files in source.items():
        target_files = destination.setdefault(rule, {})
        for path, details in files.items():
            target_files.setdefault(path, []).extend(str(item) for item in details)
            target_files[path] = sorted(set(target_files[path]))


def check_rules() -> dict[str, dict[str, list[str]]]:
    violations: dict[str, dict[str, list[str]]] = {}
    modules = _production_modules()
    for rule in RULES:
        allowed = set(rule.allowed)
        rule_violations: dict[str, list[str]] = {}
        for path in _python_files(rule.root):
            rel = path.relative_to(ROOT).as_posix()
            bad = [
                module
                for module in _imports(path, modules)
                if _matches(module, rule.forbidden) and (rel, module) not in allowed
            ]
            if bad:
                rule_violations[rel] = sorted(set(bad))
        if rule_violations:
            violations[rule.name] = rule_violations
    return violations


def _strongly_connected_components(
    graph: Mapping[str, Iterable[str]],
) -> tuple[tuple[str, ...], ...]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(graph.get(node, ())):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            current = stack.pop()
            on_stack.remove(current)
            component.append(current)
            if current == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(components, key=lambda item: (-len(item), item)))


def _graph_checks() -> dict[str, dict[str, list[str]]]:
    violations: dict[str, dict[str, list[str]]] = {}
    policy_cycles = _strongly_connected_components(AREA_DEPENDENCIES)
    if policy_cycles:
        violations["declared_area_policy_is_a_dag"] = {
            f"cycle-{index}": list(component)
            for index, component in enumerate(policy_cycles, start=1)
        }

    modules = _production_modules()
    graph: dict[str, set[str]] = {name: set() for name in modules}
    area_violations: dict[str, list[str]] = {}
    for source, record in modules.items():
        rel = record.path.relative_to(ROOT).as_posix()
        for reference in _import_references(record, modules):
            if reference.target is None or reference.target == source:
                continue
            graph[source].add(reference.target)
            target_area = modules[reference.target].area
            if target_area == record.area or target_area in AREA_DEPENDENCIES[record.area]:
                continue
            if (rel, reference.imported) in AREA_IMPORT_EXCEPTIONS:
                continue
            area_violations.setdefault(rel, []).append(
                f"{record.area}->{target_area}: {reference.imported}"
            )
    if area_violations:
        violations["imports_follow_declared_area_dag"] = {
            path: sorted(set(details)) for path, details in area_violations.items()
        }

    cycles = _strongly_connected_components(graph)
    if cycles:
        violations["production_module_graph_is_acyclic"] = {
            f"cycle-{index}": list(component) for index, component in enumerate(cycles, start=1)
        }
    return violations


def _compatibility_allowed(prefix: str, relative_path: str) -> bool:
    if relative_path == "tests/unit/test_compatibility_exports.py":
        return True
    if prefix == "chatcopilot.external_tools.shared.tool_spec":
        return relative_path.startswith("src/chatcopilot/external_tools/")
    package_paths = {
        "chatcopilot.agent.research": "src/chatcopilot/agent/research/",
        "chatcopilot.middleware.runtime.workspace": (
            "src/chatcopilot/middleware/runtime/workspace/"
        ),
    }
    allowed_root = package_paths.get(prefix)
    return allowed_root is not None and relative_path.startswith(allowed_root)


def _compatibility_import_checks() -> dict[str, dict[str, list[str]]]:
    violations: dict[str, list[str]] = {}
    modules = _production_modules()
    files = (
        tuple(_python_files(SRC))
        + tuple(_python_files(ROOT / "console"))
        + tuple(_python_files(ROOT / "tests"))
    )
    for path in files:
        if path.is_relative_to(SRC):
            name, is_package = _module_name(path, SRC, "chatcopilot")
        elif path.is_relative_to(ROOT / "console"):
            name, is_package = _module_name(path, ROOT / "console", "console")
        else:
            relative = path.relative_to(ROOT).with_suffix("")
            name = ".".join(relative.parts)
            is_package = path.name == "__init__.py"
        record = ModuleFile(name=name, path=path, area=_module_area(name), is_package=is_package)
        relative_path = path.relative_to(ROOT).as_posix()
        for reference in _import_references(record, modules):
            for prefix in COMPATIBILITY_IMPORTS:
                if not _matches(reference.imported, (prefix,)):
                    continue
                if _compatibility_allowed(prefix, relative_path):
                    continue
                violations.setdefault(relative_path, []).append(reference.imported)
    if not violations:
        return {}
    return {
        "compatibility_surfaces_are_not_internal_dependencies": {
            path: sorted(set(imports)) for path, imports in violations.items()
        }
    }


def _semantic_invariants() -> dict[str, dict[str, list[str]]]:
    violations: dict[str, dict[str, list[str]]] = {}

    turn_path = SRC / "agent" / "turn.py"
    if turn_path.exists() and "chatcopilot.agent.session" in _imports(turn_path):
        violations["session_turn_no_private_cycle"] = {
            turn_path.relative_to(ROOT).as_posix(): ["chatcopilot.agent.session"]
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
                server_path.relative_to(ROOT).as_posix(): list(forbidden)
            }
        if "already_completed" in source:
            violations["acp_pipeline_has_no_noop_completion_flag"] = {
                server_path.relative_to(ROOT).as_posix(): ["already_completed"]
            }

    gateway_path = SRC / "middleware" / "mcp" / "session_gateway.py"
    if gateway_path.exists() and "discover_tools" in gateway_path.read_text(encoding="utf-8-sig"):
        violations["codex_gateway_uses_exact_session_tools"] = {
            gateway_path.relative_to(ROOT).as_posix(): ["discover_tools"]
        }

    operations_path = ROOT / "console" / "control" / "operations.py"
    if operations_path.exists():
        tree = ast.parse(operations_path.read_text(encoding="utf-8-sig"))
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        duplicate = sorted(defined & {"follow_log", "follow_console_log"})
        if duplicate:
            violations["console_operations_has_no_observability_implementation"] = {
                operations_path.relative_to(ROOT).as_posix(): duplicate
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
                arg.value
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            }
            forbidden_git_calls.extend(sorted(literals & {"commit", "push"}))
        if forbidden_git_calls:
            violations.setdefault("repository_tasks_no_git_commit_or_push", {})[
                path.relative_to(ROOT).as_posix()
            ] = sorted(set(forbidden_git_calls))

    removed_sources = (
        SRC / "middleware" / "acp" / "code_route.py",
        SRC / "middleware" / "acp" / "route_orchestrator.py",
    )
    present = [path.relative_to(ROOT).as_posix() for path in removed_sources if path.exists()]
    if present:
        violations["removed_legacy_sources_do_not_return"] = {"repository": sorted(present)}

    private_violations: dict[str, list[str]] = {}
    modules = _production_modules()
    for record in modules.values():
        bad = list(_private_cross_area_imports(record, modules))
        if bad:
            private_violations[record.path.relative_to(ROOT).as_posix()] = bad
    if private_violations:
        violations["no_private_cross_domain_imports"] = private_violations
    return violations


def check_architecture() -> dict[str, dict[str, list[str]]]:
    violations: dict[str, dict[str, list[str]]] = {}
    _merge(violations, check_rules())
    _merge(violations, _graph_checks())
    _merge(violations, _compatibility_import_checks())
    _merge(violations, _semantic_invariants())
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    violations = check_architecture()
    if not violations:
        modules = _production_modules()
        edges = {
            (record.name, reference.target)
            for record in modules.values()
            for reference in _import_references(record, modules)
            if reference.target is not None and reference.target != record.name
        }
        print(
            f"OK: architecture boundaries ({len(modules)} modules, "
            f"{len(edges)} static edges, 0 cycles)"
        )
        return 0
    for rule, files in violations.items():
        print(f"{rule}:")
        for file, imports in files.items():
            print(f"  {file}: {', '.join(imports)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
