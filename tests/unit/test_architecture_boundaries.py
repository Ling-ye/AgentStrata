from __future__ import annotations

import ast
from pathlib import Path

import pytest


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


def test_retired_imports_are_rejected_even_from_compatibility_tests(tmp_path, monkeypatch) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("architecture_retired_imports", ROOT / "scripts/check_architecture.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source_root = tmp_path / "src/chatcopilot"
    (source_root / "agent").mkdir(parents=True)
    (source_root / "agent/current.py").write_text("from . import config\n", encoding="utf-8")
    tests = tmp_path / "tests/unit"
    tests.mkdir(parents=True)
    (tests / "test_compatibility_exports.py").write_text(
        "from chatcopilot.agent.protocol import AgentTask\n", encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SRC", source_root)

    failures = module._compatibility_import_checks()["compatibility_surfaces_are_not_internal_dependencies"]

    assert failures["src/chatcopilot/agent/current.py"] == ["chatcopilot.agent.config"]
    assert any(name.startswith("chatcopilot.agent.protocol") for name in failures["tests/unit/test_compatibility_exports.py"])


def test_empty_retired_modules_and_replacement_packages_are_rejected(tmp_path, monkeypatch) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("architecture_retired_sources", ROOT / "scripts/check_architecture.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source_root = tmp_path / "src/chatcopilot"
    for name in ("agent/config.py", "core/workspace/__init__.py", "middleware/runtime/workspace/replacement.py"):
        path = source_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SRC", source_root)

    rejected = module._semantic_invariants()["removed_legacy_sources_do_not_return"]["repository"]

    assert "src/chatcopilot/agent/config.py" in rejected
    assert "src/chatcopilot/core/workspace/__init__.py" in rejected
    assert "src/chatcopilot/middleware/runtime/workspace/replacement.py" in rejected


@pytest.fixture
def runtime_architecture_workspace(tmp_path: Path, monkeypatch):
    import importlib.util
    import sys
    from dataclasses import replace

    spec = importlib.util.spec_from_file_location(
        "check_four_layer_baseline", ROOT / "scripts/check_architecture.py"
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    spec.loader.exec_module(checker)
    original_root = checker.ROOT
    monkeypatch.setattr(checker, "RULES", tuple(
        replace(rule, root=tmp_path / rule.root.relative_to(original_root))
        for rule in checker.RULES
    ))
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "SRC", tmp_path / "src/chatcopilot")
    checker.SRC.mkdir(parents=True)
    return checker


@pytest.mark.parametrize(("source_area", "target", "rule"), (
    ("channels", "gateway.runtime", "channels_do_not_own_domain_authority"),
    ("channels", "application.actor_runtime", "channels_do_not_own_domain_authority"),
    ("channels", "authorization.policy", "channels_do_not_own_domain_authority"),
    ("application", "channels.qq_onebot.codec", "application_has_no_protocol_or_transport_implementation"),
    ("application", "gateway.runtime", "application_has_no_protocol_or_transport_implementation"),
    ("application", "protocols.acp.server", "application_has_no_protocol_or_transport_implementation"),
    ("application", "middleware.acp.server", "application_has_no_protocol_or_transport_implementation"),
    ("agent", "application.actor_runtime", "agent_no_upper_layers"),
    ("agent", "gateway.runtime", "agent_no_upper_layers"),
    ("agent", "channels.qq_onebot.codec", "agent_no_upper_layers"),
    ("agent", "botspec.model", "agent_no_upper_layers"),
    ("agent", "platforms.qq.adapter", "agent_no_upper_layers"),
    ("contracts", "agent.runtime", "contracts_is_pure"),
    ("core", "application.actor_runtime", "core_no_upper_layers"),
))
def test_four_layer_baseline_rejects_reverse_dependencies(
    runtime_architecture_workspace, source_area: str, target: str, rule: str,
) -> None:
    checker = runtime_architecture_workspace
    source = checker.SRC / source_area / "probe.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    imported = "chatcopilot." + target
    source.write_text(f"import {imported}\n", encoding="utf-8")
    target_path = checker.SRC.joinpath(*target.split(".")).with_suffix(".py")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("", encoding="utf-8")
    relative = source.relative_to(checker.ROOT).as_posix()

    assert checker.check_rules()[rule][relative] == [imported]
    assert relative in checker._graph_checks()["imports_follow_declared_area_dag"]


def test_four_layer_baseline_allows_supporting_systems_and_structured_ports(
    runtime_architecture_workspace,
) -> None:
    checker = runtime_architecture_workspace
    sources = {
        "channels/inbound.py": "from chatcopilot.contracts.gateway import CanonicalInboundEvent\n",
        "gateway/coordinator.py": "import chatcopilot.application.actor_runtime\nimport chatcopilot.channels.base\n",
        "application/actor_runtime.py": "import chatcopilot.agent.runtime\nfrom chatcopilot.contracts.resources import ResourceFetcherPort\n",
        # The instance host remains outside message responsibilities even in this directory.
        "gateway/runtime.py": "import chatcopilot.botspec.runtime\nimport chatcopilot.application.agent_runtime\nimport chatcopilot.channels.base\n",
        "protocols/acp/server.py": "import chatcopilot.protocols.gateway_client\n",
        "protocols/gateway_client.py": "import chatcopilot.contracts.gateway_rpc\n",
        "evals/isolated.py": "import chatcopilot.agent.runtime\n",
        "agent/runtime.py": "from chatcopilot.contracts.agent import AgentTask\n",
    }
    for name, body in sources.items():
        path = checker.SRC / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for name in ("contracts/gateway.py", "contracts/resources.py", "contracts/gateway_rpc.py",
                 "contracts/agent.py", "channels/base.py", "botspec/runtime.py", "application/agent_runtime.py"):
        path = checker.SRC / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    console = checker.ROOT / "console/control/query.py"
    console.parent.mkdir(parents=True)
    console.write_text("import chatcopilot.gateway.observation_queries\n", encoding="utf-8")
    (checker.SRC / "gateway/observation_queries.py").write_text("", encoding="utf-8")

    assert checker.check_rules() == {}
    assert checker._graph_checks() == {}


def test_isolated_agent_evaluation_does_not_require_channel_or_gateway_sources(
    runtime_architecture_workspace,
) -> None:
    checker = runtime_architecture_workspace
    agent = checker.SRC / "agent/runtime.py"
    agent.parent.mkdir()
    agent.write_text("", encoding="utf-8")
    evaluation = checker.SRC / "evals/trial.py"
    evaluation.parent.mkdir()
    evaluation.write_text("import chatcopilot.agent.runtime\n", encoding="utf-8")

    assert checker.check_rules() == {}
    assert checker._graph_checks() == {}
    assert not (checker.SRC / "channels").exists()
    assert not (checker.SRC / "gateway").exists()
