from __future__ import annotations

import hashlib
import re
import struct
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from ruamel.yaml import YAML
from ruamel.yaml.tokens import AliasToken, AnchorToken, TagToken

from chatcopilot.evals.manifest import load_suite_manifest
from chatcopilot.evals.suite_loader import load_suite_cases


SUITE_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "chatcopilot"
    / "evals"
    / "suites"
    / "agentstrata-capabilities-v1"
)
CASE_FIELDS = {
    "schema",
    "id",
    "version",
    "capability",
    "plugin",
    "driver",
    "preset",
    "severity",
    "turns",
    "requirements",
    "policy",
    "judge",
}
PLUGIN_DRIVERS = {
    "generic-agent": {"agent_isolated", "agent_configured"},
    "acp-scenario": {"acp_scenario"},
    "qq-live": {"qq_live"},
}
EXPECTED_CAPABILITY_COUNTS = {
    "dialogue_constraints": 2,
    "tool_orchestration": 4,
    "search": 3,
    "file_workspace": 3,
    "image_understanding": 3,
    "session_memory_subagent": 3,
    "code_recovery": 3,
    "access_security": 5,
    "qq_live": 3,
}
FORBIDDEN_KEYS = {
    "cleanup",
    "command",
    "module",
    "python_module",
    "secret",
    "shell",
    "url",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    for token in yaml.scan(text):
        assert not isinstance(token, (AliasToken, AnchorToken, TagToken))
    value = yaml.load(text)
    assert isinstance(value, dict)
    return dict(value)


def _walk(value: Any) -> Iterator[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield None, item
            yield from _walk(item)


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert payload[12:16] == b"IHDR"
    return struct.unpack(">II", payload[16:24])


def test_suite_loads_through_strict_core_contract() -> None:
    manifest = load_suite_manifest(SUITE_DIR / "manifest.yaml", suite_dir=SUITE_DIR)
    cases = load_suite_cases(manifest.suite_id, manifest=manifest)

    assert manifest.schema == 1
    assert manifest.suite_id == "agentstrata-capabilities-v1"
    assert manifest.status == "implemented"
    assert manifest.plugin_id == "generic-agent"
    assert manifest.driver_id == "agent_configured"
    assert manifest.default_preset == "quick"
    assert len(cases) == 29


def test_case_contract_counts_and_presets_are_exact() -> None:
    manifest = _load_yaml(SUITE_DIR / "manifest.yaml")
    cases = _load_yaml(SUITE_DIR / "cases.yaml")["cases"]

    assert isinstance(cases, list)
    assert len(cases) == 29
    assert Counter(case["capability"] for case in cases) == EXPECTED_CAPABILITY_COUNTS

    identifiers = [case["id"] for case in cases]
    assert len(set(identifiers)) == 29
    assert all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?", item) for item in identifiers)
    assert all(set(case) == CASE_FIELDS for case in cases)
    assert all(case["schema"] == "agentstrata-eval-case/v1" for case in cases)
    assert all(type(case["version"]) is int and case["version"] >= 1 for case in cases)
    assert {case["id"]: case["version"] for case in cases if case["version"] != 1} == {
        "access-member-owner-tool-denied": 2,
        "injection-untrusted-attachment-contained": 2,
        "injection-untrusted-search-contained": 2,
        "search-explicit-source": 2,
        "search-general-with-evidence": 2,
        "subagent-structured-result": 2,
        "tool-multistep-data-flow": 2,
        "workspace-read-fixture": 2,
        "workspace-write-contained": 2,
    }
    assert all(case["severity"] in {"required", "critical", "observational"} for case in cases)
    assert all(case["driver"] in PLUGIN_DRIVERS[case["plugin"]] for case in cases)

    presets = manifest["presets"]
    assert set(presets) == {"quick", "full", "security", "qq-live"}
    assert len(presets["quick"]["case_ids"]) == 10
    assert len(presets["full"]["case_ids"]) == 29
    assert len(presets["security"]["case_ids"]) == 5
    assert len(presets["qq-live"]["case_ids"]) == 3
    assert presets["full"]["case_ids"] == identifiers
    for preset_id, preset in presets.items():
        expected = [case["id"] for case in cases if preset_id in case["preset"]]
        assert preset["case_ids"] == expected


def test_quick_security_and_qq_live_boundaries() -> None:
    manifest = _load_yaml(SUITE_DIR / "manifest.yaml")
    cases = {case["id"]: case for case in _load_yaml(SUITE_DIR / "cases.yaml")["cases"]}
    quick = [cases[item] for item in manifest["presets"]["quick"]["case_ids"]]
    security = [cases[item] for item in manifest["presets"]["security"]["case_ids"]]
    qq_live = [cases[item] for item in manifest["presets"]["qq-live"]["case_ids"]]

    assert all(case["driver"] != "qq_live" for case in quick)
    assert all(case["policy"]["side_effect"] != "external_write" for case in quick)
    assert all(case["capability"] == "access_security" for case in security)
    assert all(case["plugin"] == "qq-live" and case["driver"] == "qq_live" for case in qq_live)
    assert all(case["policy"]["side_effect"] == "external_write" for case in qq_live)
    assert all(case["policy"]["network"] == "configured" for case in qq_live)
    for case in qq_live:
        arguments = case["judge"]["assertions"][0]["arguments"]
        assert arguments["max_messages"] == 1
        assert 1 <= arguments["max_message_chars"] <= 500


def test_workspace_write_case_pins_exact_artifact_and_isolated_delivery() -> None:
    cases = {case["id"]: case for case in _load_yaml(SUITE_DIR / "cases.yaml")["cases"]}
    case = cases["workspace-write-contained"]
    expected_sha256 = "04ae3a06e113a732bc48d9cfe13bdd7d96b0379357c27863faa6ba0630cfa526"

    assert case["requirements"] == {"tools": ["write_capability_proof", "send_files_to_user"]}
    assert case["policy"]["side_effect"] == "isolated_write"
    assert case["policy"]["network"] == "disabled"
    assert case["policy"]["allowed_tools"] == [
        "write_capability_proof",
        "send_files_to_user",
    ]
    assert case["policy"]["required_tools"] == [
        "write_capability_proof",
        "send_files_to_user",
    ]
    assert case["judge"]["assertions"][0]["arguments"] == {
        "path": "outputs/capability-proof.txt",
        "content": "AS-WORKSPACE-WRITE-17",
        "size_bytes": 21,
        "sha256": expected_sha256,
    }


def test_search_cases_pin_real_coordinator_inputs_and_structural_evidence() -> None:
    cases = {case["id"]: case for case in _load_yaml(SUITE_DIR / "cases.yaml")["cases"]}
    expected = {
        "search-general-with-evidence": {
            "objective_contains": "pathlib.Path.resolve(strict=True)",
            "expected_source_hints": ["web"],
            "expected_depth": "standard",
            "expected_verification": "none",
        },
        "search-explicit-source": {
            "objective_contains": "上海 二郎拉面 探店",
            "expected_source_hints": ["experience"],
            "expected_depth": "standard",
            "expected_verification": "none",
        },
        "search-conflict-disclosure": {
            "objective_contains": "上海 二郎拉面 地址与评价",
            "expected_source_hints": ["web", "experience"],
            "expected_depth": "thorough",
            "expected_verification": "required",
        },
    }

    for case_id, pinned in expected.items():
        case = cases[case_id]
        arguments = case["judge"]["assertions"][0]["arguments"]
        assert {key: arguments[key] for key in pinned} == pinned
        assert arguments["expected_route_source"] == "script"
        assert arguments["require_deduplication"] is True
        assert arguments["require_source_reference"] is True
        assert arguments["external_fact_correctness"] == "observational"
        prompt = case["turns"][0]["text"]
        assert pinned["objective_contains"] in prompt
        assert "search_information" in prompt
        assert "source_hints" in prompt

    conflict = cases["search-conflict-disclosure"]["judge"]["assertions"][0]["arguments"]
    assert conflict["require_cross_check"] is True
    assert conflict["require_rerank"] is True
    assert conflict["min_successful_results"] == 2


def test_cases_are_declarative_and_do_not_embed_execution_targets() -> None:
    document = _load_yaml(SUITE_DIR / "cases.yaml")

    for key, value in _walk(document):
        if key is not None:
            assert key.casefold() not in FORBIDDEN_KEYS
        if not isinstance(value, str):
            continue
        assert "http://" not in value.casefold()
        assert "https://" not in value.casefold()
        assert not value.startswith(("/", "~", "\\\\"))
        assert not re.match(r"^[A-Za-z]:[\\/]", value)
        assert "${" not in value
        assert "{{" not in value

    for case in document["cases"]:
        assert set(case["requirements"]) <= {
            "features",
            "backends",
            "tools",
            "tool_packs",
            "platforms",
            "env_keys",
        }
        assert set(case["policy"]) <= {
            "allowed_tools",
            "required_tools",
            "forbidden_tools",
            "network",
            "side_effect",
            "timeout_seconds",
        }
        assert case["judge"]["mode"] == "all"
        assert all(
            set(assertion) == {"kind", "id", "arguments"}
            and assertion["kind"] == "trusted_verifier"
            for assertion in case["judge"]["assertions"]
        )


def test_manifest_resources_are_contained_digest_pinned_and_raster_valid() -> None:
    manifest = _load_yaml(SUITE_DIR / "manifest.yaml")
    cases = _load_yaml(SUITE_DIR / "cases.yaml")["cases"]
    files = manifest["files"]

    assert len(files) == 7
    assert sum(item["role"] == "cases" for item in files) == 1
    fixture_files = [item for item in files if item["role"] == "fixture"]
    assert len(fixture_files) == 6
    assert all("resource_id" not in item for item in files if item["role"] == "cases")
    assert all(item.get("resource_id") for item in fixture_files)

    for item in files:
        relative = PurePosixPath(item["path"])
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        path = SUITE_DIR.joinpath(*relative.parts)
        assert path.is_file() and not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]

    resource_ids = {item["resource_id"] for item in fixture_files}
    referenced = {
        resource_id
        for case in cases
        for turn in case["turns"]
        for resource_id in turn.get("resources", [])
    }
    assert referenced == resource_ids

    pngs = [item for item in fixture_files if item["media_type"] == "image/png"]
    assert {item["resource_id"] for item in pngs} == {
        "order-card",
        "shape-layout",
        "sequence-first",
        "sequence-second",
    }
    for item in pngs:
        payload = (SUITE_DIR / item["path"]).read_bytes()
        width, height = _png_dimensions(payload)
        assert width >= 500 and height >= 300
        assert len(payload) < 64 * 1024


def test_image_generation_is_intentionally_not_a_case() -> None:
    cases = _load_yaml(SUITE_DIR / "cases.yaml")["cases"]
    assert all(case["capability"] != "image_generation" for case in cases)
    assert {case["id"] for case in cases if case["capability"] == "image_understanding"} == {
        "image-ocr-order-number",
        "image-shape-spatial-count",
        "image-multi-input-order",
    }


def test_code_recovery_cases_declare_atomic_eval_only_tool_surfaces() -> None:
    cases = {case["id"]: case for case in _load_yaml(SUITE_DIR / "cases.yaml")["cases"]}
    expected = {
        "code-fix-and-verify": [
            "read_eval_code",
            "edit_eval_code",
            "run_eval_code_tests",
        ],
        "code-restart-and-health": [
            "inspect_eval_service",
            "edit_eval_service",
            "run_eval_service_tests",
            "restart_eval_service",
            "probe_eval_service",
        ],
        "code-failure-no-false-success": [
            "start_code_task",
            "get_code_task",
            "cancel_code_task",
            "resume_code_task",
        ],
    }

    for case_id, tool_names in expected.items():
        case = cases[case_id]
        assert case["requirements"] == {"backends": ["native", "langgraph", "codex"]}
        assert case["policy"]["allowed_tools"] == tool_names
        assert case["policy"]["required_tools"] == tool_names
        assert "development" not in case["requirements"].get("tool_packs", [])

    lifecycle = cases["code-failure-no-false-success"]
    assert lifecycle["driver"] == "agent_configured"
    assert lifecycle["judge"]["assertions"][0]["arguments"]["expected_order"] == [
        "start_code_task",
        "get_code_task",
        "get_code_task",
        "cancel_code_task",
        "resume_code_task",
        "get_code_task",
    ]
