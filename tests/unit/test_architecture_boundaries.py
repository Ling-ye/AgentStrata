from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AGENT_FORBIDDEN_PREFIXES = (
    "chatcopilot.middleware",
    "chatcopilot.platforms",
    "chatcopilot.core.workspace",
)
EXTERNAL_TOOLS_FORBIDDEN_PREFIXES = (
    "chatcopilot.agent",
    "chatcopilot.botspec",
    "chatcopilot.middleware",
    "chatcopilot.platforms",
    "chatcopilot.core.workspace",
)
CORE_FORBIDDEN_PREFIXES = (
    "chatcopilot.agent",
    "chatcopilot.botspec",
    "chatcopilot.external_tools",
    "chatcopilot.middleware",
    "chatcopilot.platforms",
)
MIDDLEWARE_FORBIDDEN_PREFIXES = (
    "chatcopilot.platforms.feishu",
    "chatcopilot.platforms.qq",
)
CONSOLE_FORBIDDEN_PREFIXES = (
    "chatcopilot.agent.subagents",
    "chatcopilot.botspec.registry",
)


def _python_files(root: Path):
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _forbidden_imports(path: Path, prefixes: tuple[str, ...]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found: list[str] = []

    def _matches(module: str) -> bool:
        return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches(alias.name):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _matches(module):
                found.append(module)
    return found


def test_core_layer_does_not_import_upper_layers() -> None:
    violations = {
        str(path.relative_to(ROOT)): imports
        for path in _python_files(ROOT / "src" / "chatcopilot" / "core")
        if (imports := _forbidden_imports(path, CORE_FORBIDDEN_PREFIXES))
    }
    assert violations == {}


def test_agent_layer_does_not_import_middleware_or_platforms() -> None:
    violations = {
        str(path.relative_to(ROOT)): imports
        for path in _python_files(ROOT / "src" / "chatcopilot" / "agent")
        if (imports := _forbidden_imports(path, AGENT_FORBIDDEN_PREFIXES))
    }
    assert violations == {}


def test_external_tools_do_not_import_agent_botspec_middleware_or_platforms() -> None:
    violations = {
        str(path.relative_to(ROOT)): imports
        for path in _python_files(ROOT / "src" / "chatcopilot" / "external_tools")
        if (imports := _forbidden_imports(path, EXTERNAL_TOOLS_FORBIDDEN_PREFIXES))
    }
    assert violations == {}


def test_middleware_does_not_import_concrete_platform_modules() -> None:
    violations = {
        str(path.relative_to(ROOT)): imports
        for path in _python_files(ROOT / "src" / "chatcopilot" / "middleware")
        if (imports := _forbidden_imports(path, MIDDLEWARE_FORBIDDEN_PREFIXES))
    }
    assert violations == {}


def test_console_does_not_import_agent_or_botspec_internal_registries() -> None:
    violations = {
        str(path.relative_to(ROOT)): imports
        for path in _python_files(ROOT / "console")
        if (imports := _forbidden_imports(path, CONSOLE_FORBIDDEN_PREFIXES))
    }
    assert violations == {}


def test_contract_kernel_architecture_rules() -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.check_rules() == {}
