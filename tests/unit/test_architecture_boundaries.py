from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_architecture_graph_detects_strongly_connected_components() -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture_scc", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    components = module._strongly_connected_components(
        {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": set()}
    )
    assert components == (("a", "b", "c"),)


def test_architecture_graph_resolves_relative_import_base() -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture_relative", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    record = module.ModuleFile(
        name="chatcopilot.agent.feature",
        path=ROOT / "src" / "chatcopilot" / "agent" / "feature.py",
        area="agent",
    )
    node = ast.parse("from ..core import config").body[0]
    assert module._absolute_import_base(record, node) == "chatcopilot.core"


def test_architecture_graph_records_every_imported_submodule(tmp_path: Path) -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture_aliases", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source = tmp_path / "source.py"
    source.write_text("from example.routes import first, second, VALUE\n", encoding="utf-8")
    records = {
        "example.source": module.ModuleFile("example.source", source, "entrypoints"),
        "example.routes": module.ModuleFile("example.routes", tmp_path / "routes.py", "entrypoints"),
        "example.routes.first": module.ModuleFile(
            "example.routes.first", tmp_path / "first.py", "entrypoints"
        ),
        "example.routes.second": module.ModuleFile(
            "example.routes.second", tmp_path / "second.py", "entrypoints"
        ),
    }

    references = module._import_references(records["example.source"], records)

    assert tuple(reference.target for reference in references) == (
        "example.routes.first",
        "example.routes.second",
        "example.routes",
    )


def test_architecture_graph_does_not_hide_a_cycle_in_a_later_alias(tmp_path: Path) -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture_alias_cycle", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    package = tmp_path / "pkg"
    package.mkdir()
    a_path = package / "a.py"
    b_path = package / "b.py"
    helper_path = package / "helper.py"
    a_path.write_text("from pkg import helper, b\n", encoding="utf-8")
    b_path.write_text("from pkg import a\n", encoding="utf-8")
    helper_path.write_text("VALUE = 1\n", encoding="utf-8")
    records = {
        "pkg": module.ModuleFile("pkg", package / "__init__.py", "entrypoints", True),
        "pkg.a": module.ModuleFile("pkg.a", a_path, "entrypoints"),
        "pkg.b": module.ModuleFile("pkg.b", b_path, "entrypoints"),
        "pkg.helper": module.ModuleFile("pkg.helper", helper_path, "entrypoints"),
    }
    graph = {
        name: {
            reference.target
            for reference in module._import_references(record, records)
            if reference.target is not None and reference.target != name
        }
        for name, record in records.items()
        if record.path.exists()
    }

    assert module._strongly_connected_components(graph) == (("pkg.a", "pkg.b"),)


def test_private_cross_area_import_check_is_not_module_allowlist_based(tmp_path: Path) -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture_private", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source = tmp_path / "source.py"
    source.write_text(
        "from ..external_tools.example import _private, public, __all__\n",
        encoding="utf-8",
    )
    records = {
        "chatcopilot.agent.source": module.ModuleFile(
            "chatcopilot.agent.source", source, "agent"
        ),
        "chatcopilot.external_tools.example": module.ModuleFile(
            "chatcopilot.external_tools.example",
            tmp_path / "external.py",
            "external_tools",
        ),
    }

    assert module._private_cross_area_imports(
        records["chatcopilot.agent.source"], records
    ) == ("chatcopilot.external_tools.example:_private",)


def test_private_cross_area_check_covers_direct_imports_private_modules_and_console(
    tmp_path: Path,
) -> None:
    import importlib.util
    import sys

    script = ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture_private_paths", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source = tmp_path / "console_source.py"
    source.write_text(
        "import chatcopilot.external_tools._private\n"
        "from chatcopilot.external_tools._helpers import public\n",
        encoding="utf-8",
    )
    records = {
        "console.source": module.ModuleFile("console.source", source, "entrypoints"),
        "chatcopilot.external_tools._private": module.ModuleFile(
            "chatcopilot.external_tools._private",
            tmp_path / "private.py",
            "external_tools",
        ),
        "chatcopilot.external_tools._helpers": module.ModuleFile(
            "chatcopilot.external_tools._helpers",
            tmp_path / "helpers.py",
            "external_tools",
        ),
    }

    assert module._private_cross_area_imports(records["console.source"], records) == (
        "chatcopilot.external_tools._helpers",
        "chatcopilot.external_tools._private",
    )
