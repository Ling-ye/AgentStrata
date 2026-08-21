from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
from dataclasses import replace
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatcopilot.evals.implementation_catalog as implementation_catalog
import chatcopilot.evals.manifest as policy_module
import chatcopilot.evals.runner as runner_module
from chatcopilot.evals.adapters import bfcl, gaia, ifeval
from chatcopilot.evals.application.catalog import list_suite_descriptors
from chatcopilot.evals.catalog import get_suite_manifest, list_suite_manifests
from chatcopilot.evals.manifest import (
    discover_suite_manifests,
    parse_case_definitions,
    parse_suite_manifest,
    resolve_suite_preset,
    suite_definition_fingerprint,
    suite_definition_snapshot,
)
from chatcopilot.evals.models import EvalCase, JudgeResult, ManifestFile
from chatcopilot.evals.plugins import base as plugin_protocol
from chatcopilot.evals.plugins import catalog as plugin_catalog
from chatcopilot.evals.plugins import (
    CaseLoadContext,
    EvaluationPlugin,
    get_evaluation_plugin,
    list_plugin_bindings,
)
from chatcopilot.evals.plugins.catalog import PluginBinding, load_plugin_binding
from chatcopilot.evals.registry import get_cases, list_standards


@pytest.fixture
def isolated_official_suite_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep parity tests independent from machine-local benchmark configuration."""

    for key in (
        "CHATCOPILOT_GAIA_DATA_PATH",
        "CHATCOPILOT_GAIA_FILES_DIR",
        "CHATCOPILOT_GAIA_LEVELS",
        "CHATCOPILOT_GAIA_MANIFEST_PATH",
        "CHATCOPILOT_GAIA_MAX_CASES",
        "CHATCOPILOT_GAIA_CASE_PROFILE",
        "CHATCOPILOT_GAIA_SMOKE",
        "CHATCOPILOT_HF_TOKEN",
        "CHATCOPILOT_BFCL_DATA_DIR",
        "CHATCOPILOT_BFCL_CATEGORY",
        "CHATCOPILOT_BFCL_MAX_CASES",
        "CHATCOPILOT_BFCL_CASE_PROFILE",
        "CHATCOPILOT_IFEVAL_DATA_PATH",
        "CHATCOPILOT_IFEVAL_MAX_CASES",
        "CHATCOPILOT_IFEVAL_CASE_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CHATCOPILOT_EVALS_DATA_DIR", str(tmp_path / "official-cache"))
    monkeypatch.setattr(gaia, "_DEFAULT_CACHE_DIR", tmp_path / "gaia-cache")


def _manifest_text(extra: str = "") -> bytes:
    return (
        "schema: 1\n"
        "suite_id: demo-suite\n"
        "version: 1.0.0\n"
        "name: Demo Suite\n"
        "kind: agent\n"
        "status: implemented\n"
        "value: Exercises a demo capability.\n"
        "recommendation: Run after demo changes.\n"
        "cadence: regression\n"
        "plugin_id: generic-agent\n"
        "driver_id: agent_isolated\n"
        f"{extra}"
    ).encode("utf-8")


def _case_text(
    *,
    text: str = "Observe the configured runtime without mutation.",
    timeout: str = "30",
    arguments: str = "{}",
    plugin: str = "generic-agent",
    driver: str = "agent_isolated",
    side_effect: str = "none",
    network: str = "disabled",
) -> bytes:
    return f"""
cases:
  - schema: agentstrata-eval-case/v1
    id: strict-case
    version: 1
    capability: runtime_observation
    plugin: {plugin}
    driver: {driver}
    preset: [full]
    severity: required
    turns:
      - text: {text}
    requirements: {{}}
    policy:
      side_effect: {side_effect}
      network: {network}
      timeout_seconds: {timeout}
    judge:
      mode: all
      assertions:
        - kind: trusted_verifier
          id: observation_recorded
          arguments: {arguments}
""".encode()


def test_manifest_parser_rejects_unknown_and_duplicate_fields() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        parse_suite_manifest(_manifest_text("python_module: untrusted.module\n"))
    with pytest.raises(ValueError, match="invalid YAML"):
        parse_suite_manifest(_manifest_text("name: Duplicate\n"))


def test_manifest_parser_rejects_aliases_and_path_traversal() -> None:
    with pytest.raises(ValueError, match="aliases, anchors and tags"):
        parse_suite_manifest(_manifest_text("setup_hint: &shared unsafe\n"))
    with pytest.raises(ValueError, match="contained below"):
        parse_suite_manifest(
            _manifest_text(
                "files:\n"
                "  - path: ../outside.yaml\n"
                "    role: cases\n"
                "    media_type: application/yaml\n"
                f"    sha256: {'0' * 64}\n"
            )
        )


def test_packaged_manifest_discovery_verifies_resource_digest(tmp_path) -> None:
    suite_dir = tmp_path / "demo-suite"
    suite_dir.mkdir()
    cases = b"cases: []\n"
    (suite_dir / "cases.yaml").write_bytes(cases)
    digest = hashlib.sha256(cases).hexdigest()
    (suite_dir / "manifest.yaml").write_bytes(
        _manifest_text(
            "files:\n"
            "  - path: cases.yaml\n"
            "    role: cases\n"
            "    media_type: application/yaml\n"
            f"    sha256: {digest}\n"
            "presets:\n"
            "  quick:\n"
            "    description: Fast deterministic coverage.\n"
            "    case_ids: [case-a]\n"
            "default_preset: quick\n"
        )
    )

    manifests = discover_suite_manifests(tmp_path)
    assert [item.suite_id for item in manifests] == ["demo-suite"]
    assert manifests[0].files[0].sha256 == digest

    (suite_dir / "cases.yaml").write_text("cases: [drift]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        discover_suite_manifests(tmp_path)


def test_manifest_loader_rejects_a_symlinked_resource_parent(tmp_path: Path) -> None:
    suites_root = tmp_path / "suites"
    suite_dir = suites_root / "demo-suite"
    outside = tmp_path / "outside-fixtures"
    suite_dir.mkdir(parents=True)
    outside.mkdir()
    fixture = outside / "note.txt"
    fixture.write_text("trusted fixture\n", encoding="utf-8")
    (suite_dir / "fixtures").symlink_to(outside, target_is_directory=True)
    digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    (suite_dir / "manifest.yaml").write_bytes(
        _manifest_text(
            "files:\n"
            "  - path: fixtures/note.txt\n"
            "    role: fixture\n"
            "    resource_id: note\n"
            "    media_type: text/plain\n"
            f"    sha256: {digest}\n"
        )
    )

    with pytest.raises(ValueError, match="ancestor must not be a symlink"):
        discover_suite_manifests(suites_root)


def test_manifest_discovery_rejects_a_symlinked_suite_root(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual-suites"
    actual_root.mkdir()
    linked_root = tmp_path / "linked-suites"
    linked_root.symlink_to(actual_root, target_is_directory=True)

    with pytest.raises(ValueError, match="root must not be a symlink"):
        discover_suite_manifests(linked_root)


def test_manifest_loader_rejects_a_hard_linked_resource(tmp_path: Path) -> None:
    suites_root = tmp_path / "suites"
    suite_dir = suites_root / "demo-suite"
    suite_dir.mkdir(parents=True)
    outside = tmp_path / "outside-cases.yaml"
    cases = b"cases: []\n"
    outside.write_bytes(cases)
    os.link(outside, suite_dir / "cases.yaml")
    digest = hashlib.sha256(cases).hexdigest()
    (suite_dir / "manifest.yaml").write_bytes(
        _manifest_text(
            "files:\n"
            "  - path: cases.yaml\n"
            "    role: cases\n"
            "    media_type: application/yaml\n"
            f"    sha256: {digest}\n"
        )
    )

    with pytest.raises(ValueError, match="single-link regular file"):
        discover_suite_manifests(suites_root)


def test_manifest_loader_rejects_toctou_replacement_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suites_root = tmp_path / "suites"
    suite_dir = suites_root / "demo-suite"
    suite_dir.mkdir(parents=True)
    manifest_path = suite_dir / "manifest.yaml"
    original = _manifest_text()
    manifest_path.write_bytes(original)
    real_read = policy_module.os.read
    replaced = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        payload = real_read(descriptor, size)
        if payload and not replaced:
            replaced = True
            replacement = suite_dir / ".replacement.yaml"
            replacement.write_bytes(original.replace(b"1.0.0", b"1.0.1"))
            replacement.replace(manifest_path)
        return payload

    monkeypatch.setattr(policy_module.os, "read", racing_read)

    with pytest.raises(ValueError, match="changed while it was being read"):
        discover_suite_manifests(suites_root)


def test_manifest_loader_rejects_same_inode_timestamp_drift_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suites_root = tmp_path / "suites"
    suite_dir = suites_root / "demo-suite"
    suite_dir.mkdir(parents=True)
    manifest_path = suite_dir / "manifest.yaml"
    original = _manifest_text()
    manifest_path.write_bytes(original)
    initial = manifest_path.stat(follow_symlinks=False)
    real_read = policy_module.os.read
    modified = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal modified
        payload = real_read(descriptor, size)
        if payload and not modified:
            modified = True
            manifest_path.write_bytes(original.replace(b"1.0.0", b"1.0.1"))
            os.utime(
                manifest_path,
                ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000),
            )
        return payload

    monkeypatch.setattr(policy_module.os, "read", racing_read)

    with pytest.raises(ValueError, match="changed while it was being read"):
        discover_suite_manifests(suites_root)


def test_capability_case_contract_is_strict_and_typed() -> None:
    resource = (
        resources.files("chatcopilot.evals")
        .joinpath("suites")
        .joinpath("agentstrata-capabilities-v1")
        .joinpath("cases.yaml")
    )
    definitions = parse_case_definitions(resource.read_bytes(), source="capability/cases.yaml")
    assert len(definitions) == 26
    by_id = {item.case_id: item for item in definitions}
    acp_case = by_id["access-nickname-spoof-denied"]
    assert acp_case.plugin_id == "acp-scenario"
    assert acp_case.driver_id == "acp_scenario"
    assert acp_case.policy.side_effect == "isolated_read"
    assert acp_case.assertions[0].kind == "trusted_verifier"
    assert all(item.policy.side_effect != "external_write" for item in definitions)


@pytest.mark.parametrize("timeout", [".nan", ".inf", "-.inf"])
def test_case_timeout_rejects_non_finite_numbers(timeout: str) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        parse_case_definitions(_case_text(timeout=timeout), source="non-finite.yaml")


def test_assertion_arguments_are_bounded_canonical_json() -> None:
    definition = parse_case_definitions(
        _case_text(arguments="{z: [1, {ok: true}], a: null}"),
        source="canonical-arguments.yaml",
    )[0]

    assert list(definition.assertions[0].arguments) == ["a", "z"]
    assert definition.assertions[0].arguments == {
        "a": None,
        "z": [1, {"ok": True}],
    }

    nested: object = "leaf"
    for _ in range(10):
        nested = [nested]
    with pytest.raises(ValueError, match="maximum JSON depth"):
        parse_case_definitions(
            _case_text(arguments=json.dumps({"value": nested})),
            source="deep-arguments.yaml",
        )

    with pytest.raises(ValueError, match="list items"):
        parse_case_definitions(
            _case_text(arguments=json.dumps({"value": list(range(257))})),
            source="large-arguments.yaml",
        )


_WINDOWS_ABSOLUTE_PATH = "C:" + "\\" + "Users" + "\\demo\\secret.txt"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ("{value: .nan}", "non-finite"),
        ("{value: 'http://example.invalid/data'}", "HTTP URL"),
        ("{value: '/etc/passwd'}", "absolute path"),
        (f"{{value: '{_WINDOWS_ABSOLUTE_PATH}'}}", "absolute path"),
        ("{value: '//server/share/file.txt'}", "absolute path"),
        ("{api_key: redacted}", "secret-like key"),
        ("{value: 'sk-examplecredential123'}", "secret-like value"),
        ("{value: 'opaqueCredentialValue0123456789ABCDEF'}", "secret-like value"),
    ],
)
def test_assertion_arguments_reject_urls_paths_secrets_and_non_finite_values(
    arguments: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_case_definitions(
            _case_text(arguments=arguments),
            source="unsafe-arguments.yaml",
        )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("'Visit http://example.invalid/data.'", "HTTP URL"),
        ("'Read /etc/passwd.'", "absolute path"),
        (f"'Read {_WINDOWS_ABSOLUTE_PATH}.'", "absolute path"),
        (f"'读取{_WINDOWS_ABSOLUTE_PATH}。'", "absolute path"),
        ("'Use api_key=sk-examplecredential123.'", "secret-like value"),
        ("'使用api_key=sk-examplecredential123。'", "secret-like value"),
    ],
)
def test_case_turn_text_rejects_urls_absolute_paths_and_secret_values(
    text: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_case_definitions(
            _case_text(text=text),
            source="unsafe-turn.yaml",
        )


def test_agent_evaluation_case_rejects_external_write() -> None:
    with pytest.raises(ValueError, match="Agent Evaluation cases cannot use external_write"):
        parse_case_definitions(
            _case_text(side_effect="external_write", network="configured"),
            source="generic-external-write.yaml",
        )


def test_case_schema_supports_backend_env_and_observational_severity() -> None:
    definitions = parse_case_definitions(
        b"""
cases:
  - schema: agentstrata-eval-case/v1
    id: observational-case
    version: 1
    capability: runtime_observation
    plugin: generic-agent
    driver: agent_configured
    preset: [full]
    severity: observational
    turns:
      - text: Observe the configured runtime without mutation.
    requirements:
      backends: [native, codex]
      env_keys: [CHATCOPILOT_EVAL_TEST_VALUE]
    policy:
      side_effect: none
      network: disabled
      timeout_seconds: 30
    judge:
      mode: all
      assertions:
        - kind: trusted_verifier
          id: observation_recorded
""",
        source="observational.yaml",
    )
    assert definitions[0].severity == "observational"
    assert definitions[0].requirements.backends == ("native", "codex")
    assert definitions[0].requirements.env_keys == ("CHATCOPILOT_EVAL_TEST_VALUE",)


def test_static_plugin_bindings_are_exact_and_loadable() -> None:
    bindings = list_plugin_bindings()
    assert {item.plugin_id for item in bindings} >= {
        "gaia",
        "bfcl",
        "ifeval",
        "generic-agent",
        "acp-scenario",
    }
    for binding in bindings:
        plugin = get_evaluation_plugin(binding.plugin_id)
        assert plugin.plugin_id == binding.plugin_id
        assert plugin.implementation_module == binding.module
        assert plugin.allowed_drivers == binding.allowed_drivers

    invalid = PluginBinding(
        plugin_id="invalid",
        module="outside.plugins.invalid",
        api_version="1",
        allowed_drivers=frozenset({"dry_run"}),
    )
    with pytest.raises(ValueError, match="not in the static catalog"):
        load_plugin_binding(invalid)


def test_legacy_catalog_facade_and_thin_plugins_remain_compatible(
    monkeypatch: pytest.MonkeyPatch,
    isolated_official_suite_env: None,
) -> None:
    del isolated_official_suite_env
    assert {item.suite_id for item in list_standards()} >= {
        "gaia",
        "bfcl",
        "ifeval",
        "agentstrata-canary-self-update-v1",
        "swe-bench-verified",
        "webarena",
    }
    assert get_suite_manifest("bfcl").driver_id == "direct_llm"
    assert get_suite_manifest("swe-bench-verified").status == "planned"
    assert get_suite_manifest("agentstrata-canary-self-update-v1").status == "planned"
    assert get_cases("agentstrata-canary-self-update-v1", auto_prepare=False) == ()
    assert get_cases("swe-bench-verified", auto_prepare=False) == ()
    assert get_cases("bfcl", auto_prepare=False)
    assert get_cases("ifeval", auto_prepare=False)

    monkeypatch.setenv("CHATCOPILOT_GAIA_SMOKE", "1")
    assert get_cases("gaia", auto_prepare=False)


def test_official_plugins_preserve_adapter_case_selection(
    monkeypatch: pytest.MonkeyPatch,
    isolated_official_suite_env: None,
) -> None:
    del isolated_official_suite_env
    monkeypatch.setenv("CHATCOPILOT_GAIA_SMOKE", "1")
    adapter_loaders = {
        "gaia": lambda: gaia.load_cases(auto_download=False),
        "bfcl": bfcl.load_cases,
        "ifeval": ifeval.load_cases,
    }

    for suite_id, load_adapter_cases in adapter_loaders.items():
        manifest = get_suite_manifest(suite_id)
        plugin = get_evaluation_plugin(manifest.plugin_id)
        context = CaseLoadContext(manifest=manifest, auto_prepare=False, options={})
        adapter_cases = load_adapter_cases()
        plugin_cases = plugin.load_cases(context)
        registry_cases = get_cases(suite_id, auto_prepare=False)

        assert plugin_cases == adapter_cases, suite_id
        assert registry_cases == adapter_cases, suite_id
        assert [case.case_id for case in plugin_cases] == [case.case_id for case in adapter_cases]

    bfcl_manifest = get_suite_manifest("bfcl")
    bfcl_plugin = get_evaluation_plugin("bfcl")
    selected = bfcl_plugin.load_cases(
        CaseLoadContext(
            manifest=bfcl_manifest,
            auto_prepare=False,
            options={"category": "simple"},
        )
    )
    assert selected == bfcl.load_cases(category="simple")
    assert selected and all(case.metadata["bfcl_category"] == "simple" for case in selected)


def test_official_plugins_preserve_adapter_judges(
    monkeypatch: pytest.MonkeyPatch,
    isolated_official_suite_env: None,
) -> None:
    del isolated_official_suite_env
    monkeypatch.setenv("CHATCOPILOT_GAIA_SMOKE", "1")

    gaia_case = gaia.load_cases(auto_download=False)[0]
    gaia_plugin = get_evaluation_plugin("gaia")
    assert gaia_plugin.judge is not None
    for final_text in ("Final answer: 42.", "Final answer: 41."):
        assert gaia_plugin.judge(gaia_case, final_text, chat_config=None) == gaia.judge(
            gaia_case, final_text
        )

    ifeval_case = ifeval.load_cases()[0]
    ifeval_plugin = get_evaluation_plugin("ifeval")
    assert ifeval_plugin.judge is not None
    for final_text in ("固定测试集适合观察优化", "固定测试集适合观察，优化"):
        assert ifeval_plugin.judge(ifeval_case, final_text, chat_config=None) == ifeval.judge(
            ifeval_case, final_text
        )

    bfcl_case = bfcl.load_cases()[0]
    bfcl_plugin = get_evaluation_plugin("bfcl")
    assert bfcl_plugin.judge is not None
    for tool_calls in (
        bfcl_case.metadata["expected_calls"],
        [{"name": "wrong_function", "arguments": {"value": 1}}],
    ):
        assert bfcl_plugin.judge(bfcl_case, {"tool_calls": tool_calls}) == bfcl.judge(
            bfcl_case, tool_calls
        )


def test_bfcl_plugin_preserves_request_and_execution_metadata(
    monkeypatch: pytest.MonkeyPatch,
    isolated_official_suite_env: None,
) -> None:
    del isolated_official_suite_env
    case = bfcl.load_cases()[0]
    captured: dict[str, object] = {}

    class FakeLLMClient:
        def __init__(self, config: object) -> None:
            captured["config"] = config

        def chat(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return SimpleNamespace(
                content="synthetic BFCL response",
                tool_calls=case.metadata["expected_calls"],
                usage={"prompt_tokens": 11, "completion_tokens": 3},
            )

    monkeypatch.setattr("chatcopilot.core.llm_client.LLMClient", FakeLLMClient)
    plugin = get_evaluation_plugin("bfcl")
    assert plugin.execute_trial is not None

    observation = plugin.execute_trial(
        case,
        chat_config=SimpleNamespace(llm="configured-chat-profile"),
    )

    assert captured["config"] == "configured-chat-profile"
    assert captured["messages"] == bfcl.build_messages(case)
    assert captured["tools"] == bfcl.build_tools_schema(case)
    assert captured["stream"] is False
    assert observation == {
        "final_text": "synthetic BFCL response",
        "tool_calls": case.metadata["expected_calls"],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        "metadata": {
            "bfcl_category": case.metadata["bfcl_category"],
            "benchmark_category": case.metadata["bfcl_category"],
        },
    }


def test_direct_llm_runner_uses_non_bfcl_plugin_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = EvalCase(
        case_id="synthetic-direct-case",
        input="synthetic input",
        category="synthetic",
        expected_behavior="use synthetic hooks",
    )
    calls: list[tuple[str, str]] = []

    def execute_trial(selected: EvalCase, *, chat_config: object) -> dict[str, object]:
        assert chat_config is not None
        calls.append(("execute", selected.case_id))
        return {
            "final_text": "synthetic output",
            "tool_calls": [{"name": "synthetic_tool", "arguments": {"value": 7}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            "metadata": {
                "benchmark_category": "synthetic-protocol",
                "synthetic_receipt": True,
            },
        }

    def judge(selected: EvalCase, observation: dict[str, object]) -> JudgeResult:
        calls.append(("judge", selected.case_id))
        assert observation["final_text"] == "synthetic output"
        return JudgeResult(score=1.0, max_score=1.0, passed=True)

    plugin = EvaluationPlugin(
        plugin_id="synthetic-direct",
        api_version=plugin_protocol.PLUGIN_API_VERSION,
        implementation_module="chatcopilot.evals.plugins.synthetic_direct",
        allowed_drivers=frozenset({"direct_llm"}),
        load_cases=lambda _context: (case,),
        execute_trial=execute_trial,
        judge=judge,
    )
    monkeypatch.setattr(runner_module, "_load_bot_config", lambda _bot: object())
    monkeypatch.setattr(
        bfcl,
        "judge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("BFCL judge must not run")),
    )

    results = runner_module._run_direct_llm_cases(
        "synthetic-suite",
        plugin,
        (case,),
        bot="synthetic-bot",
    )

    assert calls == [
        ("execute", "synthetic-direct-case"),
        ("judge", "synthetic-direct-case"),
    ]
    assert len(results) == 1
    assert results[0].suite_id == "synthetic-suite"
    assert results[0].status == "passed"
    assert results[0].metadata == {
        "benchmark_category": "synthetic-protocol",
        "synthetic_receipt": True,
        "usage_totals": {"prompt_tokens": 3, "completion_tokens": 2},
        "tool_calls": [{"name": "synthetic_tool", "arguments": {"value": 7}}],
    }
    assert "bfcl_category" not in results[0].metadata
    leaderboard = runner_module._leaderboard_format(tuple(results), 1.0)
    assert leaderboard is not None
    assert leaderboard["accuracy_synthetic-protocol"] == 1.0


@pytest.mark.parametrize("missing_hook", ("execute_trial", "judge"))
def test_direct_llm_runner_fails_closed_when_required_hook_is_missing(
    missing_hook: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = EvalCase("case-a", "input", "synthetic", "expected")
    plugin = EvaluationPlugin(
        plugin_id="synthetic-direct",
        api_version=plugin_protocol.PLUGIN_API_VERSION,
        implementation_module="chatcopilot.evals.plugins.synthetic_direct",
        allowed_drivers=frozenset({"direct_llm"}),
        load_cases=lambda _context: (case,),
        execute_trial=lambda _case, **_kwargs: {
            "final_text": "output",
            "tool_calls": [],
            "usage": {},
        },
        judge=lambda _case, _observation: JudgeResult(1.0, 1.0, True),
    )
    plugin = replace(plugin, **{missing_hook: None})
    monkeypatch.setattr(
        runner_module,
        "_load_bot_config",
        lambda _bot: (_ for _ in ()).throw(AssertionError("Bot config must not load")),
    )

    with pytest.raises(ValueError, match="must define execute_trial and judge hooks"):
        runner_module._run_direct_llm_cases(
            "synthetic-suite",
            plugin,
            (case,),
            bot="synthetic-bot",
        )


def test_agent_runner_fails_closed_when_judge_hook_is_missing() -> None:
    case = EvalCase("case-a", "input", "synthetic", "expected")
    plugin = EvaluationPlugin(
        plugin_id="synthetic-agent",
        api_version=plugin_protocol.PLUGIN_API_VERSION,
        implementation_module="chatcopilot.evals.plugins.synthetic_agent",
        allowed_drivers=frozenset({"agent_configured"}),
        load_cases=lambda _context: (case,),
    )

    with pytest.raises(ValueError, match="must define a deterministic judge hook"):
        runner_module._judge_case(plugin, case, "claimed pass")


def test_runner_has_no_official_suite_identity_branches() -> None:
    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    official_ids = {"gaia", "bfcl", "ifeval"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.IfExp)):
            continue
        names = {item.id for item in ast.walk(node.test) if isinstance(item, ast.Name)}
        literals = {
            item.value
            for item in ast.walk(node.test)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if "suite_id" in names and literals.intersection(official_ids):
            offenders.append(ast.unparse(node.test))

    assert offenders == []
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "chatcopilot.evals.adapters"
        for node in ast.walk(tree)
    )


def test_planned_suites_are_explicitly_unavailable() -> None:
    descriptors = {item["suite_id"]: item for item in list_suite_descriptors()}
    for suite_id in ("swe-bench-verified", "webarena"):
        assert descriptors[suite_id]["status"] == "planned"
        assert descriptors[suite_id]["implemented"] is False
        assert descriptors[suite_id]["ready"] is False
        assert "尚未实现" in descriptors[suite_id]["unavailable_reason"]


def test_preset_resolution_and_definition_fingerprint_are_deterministic() -> None:
    manifest = parse_suite_manifest(
        _manifest_text("presets:\n  quick:\n    case_ids: [case-a]\ndefault_preset: quick\n")
    )
    assert resolve_suite_preset(
        manifest,
        available_case_ids=("case-a", "case-b"),
    ) == ("case-a",)
    assert resolve_suite_preset(
        manifest,
        preset="custom",
        case_ids=("case-b",),
        available_case_ids=("case-a", "case-b"),
    ) == ("case-b",)
    with pytest.raises(ValueError, match="unknown case ids"):
        resolve_suite_preset(
            manifest,
            preset="custom",
            case_ids=("missing",),
            available_case_ids=("case-a",),
        )

    plugin = get_evaluation_plugin("generic-agent")
    case = EvalCase("case-a", "hello", "demo", "reply")
    first = suite_definition_fingerprint(manifest, plugin, (case,))
    second = suite_definition_fingerprint(manifest, plugin, (case,))
    drifted = suite_definition_fingerprint(
        manifest,
        plugin,
        (replace(case, input="changed"),),
    )
    assert first == second
    assert first != drifted
    assert len(first) == 64


def test_definition_fingerprint_covers_every_execution_definition_axis(monkeypatch) -> None:
    manifest = parse_suite_manifest(_manifest_text())
    plugin = get_evaluation_plugin("generic-agent")
    case = EvalCase("case-a", "hello", "demo", "reply")
    target = {"executor": "native", "model": "commercial-a"}
    baseline = suite_definition_fingerprint(
        manifest,
        plugin,
        (case,),
        target_fingerprint=target,
    )

    assert baseline != suite_definition_fingerprint(
        replace(manifest, version="1.0.1"), plugin, (case,), target_fingerprint=target
    )
    assert baseline != suite_definition_fingerprint(
        manifest, plugin, (replace(case, input="changed"),), target_fingerprint=target
    )
    fixture_manifest = replace(
        manifest,
        files=(
            ManifestFile(
                path="fixture.txt",
                role="fixture",
                media_type="text/plain",
                sha256="1" * 64,
                resource_id="fixture",
            ),
        ),
    )
    fixture_drift = replace(
        fixture_manifest,
        files=(replace(fixture_manifest.files[0], sha256="2" * 64),),
    )
    assert suite_definition_fingerprint(fixture_manifest, plugin, (case,)) != (
        suite_definition_fingerprint(fixture_drift, plugin, (case,))
    )
    assert baseline != suite_definition_fingerprint(
        replace(manifest, driver_id="agent_configured"),
        plugin,
        (case,),
        target_fingerprint=target,
    )
    assert baseline != suite_definition_fingerprint(
        manifest,
        plugin,
        (case,),
        target_fingerprint={"executor": "native", "model": "commercial-b"},
    )

    original_digest = plugin_catalog.plugin_implementation_sha256
    monkeypatch.setattr(
        plugin_catalog,
        "plugin_implementation_sha256",
        lambda plugin_id: "f" * 64,
    )
    assert baseline != suite_definition_fingerprint(
        manifest, plugin, (case,), target_fingerprint=target
    )
    monkeypatch.setattr(plugin_catalog, "plugin_implementation_sha256", original_digest)

    original_driver = plugin_protocol.DRIVER_PROTOCOL_VERSION
    monkeypatch.setattr(plugin_protocol, "DRIVER_PROTOCOL_VERSION", "agentstrata-eval-driver/v2")
    assert baseline != suite_definition_fingerprint(
        manifest, plugin, (case,), target_fingerprint=target
    )
    monkeypatch.setattr(plugin_protocol, "DRIVER_PROTOCOL_VERSION", original_driver)

    original_scorer = plugin_protocol.SCORER_PROTOCOL_VERSION
    monkeypatch.setattr(plugin_protocol, "SCORER_PROTOCOL_VERSION", "agentstrata-eval-scorer/v2")
    assert baseline != suite_definition_fingerprint(
        manifest, plugin, (case,), target_fingerprint=target
    )
    monkeypatch.setattr(plugin_protocol, "SCORER_PROTOCOL_VERSION", original_scorer)

    binding = plugin_catalog.get_plugin_binding("generic-agent")
    changed_binding = replace(binding, binding_version="agentstrata-plugin-binding/v2")
    changed_bindings = tuple(
        changed_binding if item.plugin_id == binding.plugin_id else item
        for item in plugin_catalog._BINDINGS
    )
    monkeypatch.setattr(plugin_catalog, "_BINDINGS", changed_bindings)
    monkeypatch.setitem(plugin_catalog._BY_ID, binding.plugin_id, changed_binding)
    assert baseline != suite_definition_fingerprint(
        manifest, plugin, (case,), target_fingerprint=target
    )


def test_definition_fingerprint_covers_actual_driver_and_scorer_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = parse_suite_manifest(_manifest_text())
    plugin = get_evaluation_plugin("generic-agent")
    case = EvalCase("case-a", "hello", "demo", "reply")
    baseline = suite_definition_fingerprint(manifest, plugin, (case,))
    original = implementation_catalog.trusted_module_sha256
    modules = suite_definition_snapshot(manifest, plugin, (case,))["execution_implementations"][
        "modules"
    ]

    for changed_module in modules:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                implementation_catalog,
                "trusted_module_sha256",
                lambda module_name, changed_module=changed_module: (
                    hashlib.sha256(f"drift:{module_name}".encode()).hexdigest()
                    if module_name == changed_module
                    else original(module_name)
                ),
            )
            assert baseline != suite_definition_fingerprint(manifest, plugin, (case,))


@pytest.mark.parametrize(
    ("suite_id", "implementation_module"),
    (
        ("gaia", "chatcopilot.evals.adapters.gaia"),
        ("ifeval", "chatcopilot.evals.adapters.ifeval"),
        ("bfcl", "chatcopilot.evals.adapters.bfcl"),
    ),
)
def test_official_definition_snapshot_hashes_adapter_and_runner(
    suite_id: str,
    implementation_module: str,
) -> None:
    manifest = get_suite_manifest(suite_id)
    plugin = get_evaluation_plugin(manifest.plugin_id)
    snapshot = suite_definition_snapshot(manifest, plugin, ())
    modules = snapshot["execution_implementations"]["modules"]

    assert implementation_module in modules
    assert "chatcopilot.evals.runner" in modules


def test_product_definition_snapshot_hashes_each_selected_execution_layer() -> None:
    manifest = get_suite_manifest("agentstrata-capabilities-v1")
    plugin = get_evaluation_plugin(manifest.plugin_id)
    selected_ids = {
        "dialogue-strict-json",
        "access-nickname-spoof-denied",
        "attachment-remote-reference-not-local",
    }
    cases = tuple(case for case in get_cases(manifest.suite_id) if case.case_id in selected_ids)

    snapshot = suite_definition_snapshot(manifest, plugin, cases)
    modules = snapshot["execution_implementations"]["modules"]

    assert len(cases) == len(selected_ids)
    assert {
        "chatcopilot.evals.runner",
        "chatcopilot.evals.capability_executor",
        "chatcopilot.evals.capability_verifiers",
        "chatcopilot.evals.capability_scenarios",
    }.issubset(modules)
    assert set(snapshot["case_plugin_bindings"]) == {
        "acp-scenario",
        "generic-agent",
    }


def test_comparison_implementation_snapshot_covers_executor_profile_and_scorers() -> None:
    snapshot = implementation_catalog.comparison_implementation_snapshot()

    assert {
        "chatcopilot.evals.isolated_executor",
        "chatcopilot.evals.profiles",
        "chatcopilot.evals.runner",
        "chatcopilot.evals.adapters.gaia",
        "chatcopilot.evals.adapters.ifeval",
    }.issubset(snapshot["modules"])


def test_runtime_implementation_snapshot_covers_real_agent_and_capability_dependencies() -> None:
    native = implementation_catalog.runtime_implementation_snapshot("native")
    codex = implementation_catalog.runtime_implementation_snapshot("codex")
    common_modules = {
        "chatcopilot.agent.runtime",
        "chatcopilot.agent.turn",
        "chatcopilot.agent.tools.registry",
        "chatcopilot.agent.tools.executor",
        "chatcopilot.agent.search.coordinator",
        "chatcopilot.agent.search.router",
        "chatcopilot.agent.search.providers",
        "chatcopilot.agent.subagents.registry",
        "chatcopilot.agent.subagents.runner",
        "chatcopilot.agent.subagents.result",
        "chatcopilot.middleware.acp.access_gate",
        "chatcopilot.middleware.acp.agent_bridge",
        "chatcopilot.platforms.qq.at_proxy",
    }

    assert common_modules.issubset(native["modules"])
    assert common_modules.issubset(codex["modules"])
    assert "chatcopilot.agent.backends.inprocess" in native["modules"]
    assert "chatcopilot.agent.backends.codex" in codex["modules"]
    assert "chatcopilot.agent.backends.session_relay" in codex["modules"]
    assert "chatcopilot.agent.backends.session_relay" not in native["modules"]


def test_runner_executes_parent_frozen_case_without_reloading_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = get_cases("ifeval")[0]
    plugin = get_evaluation_plugin("ifeval")
    frozen_plugin = replace(
        plugin,
        load_cases=lambda _context: pytest.fail("frozen Trial must not reload Cases"),
    )
    monkeypatch.setattr(
        runner_module,
        "get_evaluation_plugin",
        lambda _plugin_id: frozen_plugin,
    )

    result = runner_module.run_suite(
        "ifeval",
        dry_run=True,
        case_ids=(case.case_id,),
        _frozen_cases=(case,),
    )

    assert [item.case_id for item in result.cases] == [case.case_id]
    assert result.cases[0].status == "skipped"


def test_implementation_digest_reads_static_source_without_importing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "driver.py"
    source.write_text("raise RuntimeError('must not be imported')\n", encoding="utf-8")
    monkeypatch.setattr(implementation_catalog, "_EVALS_ROOT", tmp_path)

    assert (
        implementation_catalog.trusted_module_sha256("chatcopilot.evals.driver")
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )


def test_implementation_digest_rejects_symlink_parent_and_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "evals"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    (external / "driver.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "unsafe").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(implementation_catalog, "_EVALS_ROOT", root)

    with pytest.raises(ValueError, match="parent is unsafe"):
        implementation_catalog.trusted_module_sha256("chatcopilot.evals.unsafe.driver")

    original = root / "driver.py"
    linked = root / "linked.py"
    original.write_text("VALUE = 2\n", encoding="utf-8")
    os.link(original, linked)
    with pytest.raises(ValueError, match="inode is unsafe"):
        implementation_catalog.trusted_module_sha256("chatcopilot.evals.driver")


def test_plugin_source_digest_reads_exact_source_and_fails_closed(monkeypatch, tmp_path) -> None:
    plugin = get_evaluation_plugin("generic-agent")
    module = importlib.import_module(plugin.implementation_module)
    source_path = Path(str(module.__file__))
    expected = hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert plugin_catalog.plugin_implementation_sha256(plugin.plugin_id) == expected

    monkeypatch.setattr(module, "__file__", str(tmp_path / "missing.py"))
    with pytest.raises(ValueError, match="unavailable"):
        plugin_catalog.plugin_implementation_sha256(plugin.plugin_id)


def test_all_implemented_manifests_have_trusted_driver_bindings() -> None:
    for manifest in list_suite_manifests():
        if manifest.status == "planned":
            continue
        plugin = get_evaluation_plugin(manifest.plugin_id)
        assert manifest.driver_id in plugin.allowed_drivers
