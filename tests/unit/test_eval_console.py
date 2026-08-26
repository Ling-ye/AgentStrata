from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from chatcopilot.evals.adapters import gaia
from chatcopilot.evals.application.bots import (
    EvaluationBotRef,
    bot_env,
    evaluation_subprocess_env,
    temporary_eval_env,
)
from chatcopilot.evals.application.catalog import (
    list_case_summaries,
    list_profile_descriptors,
    list_suite_descriptors,
    stream_prepare_suite,
)
from chatcopilot.evals.env import normalize_eval_env
from chatcopilot.evals.official_data import bfcl_cache_dir, ifeval_cache_path
from chatcopilot.evals.registry import get_cases
from chatcopilot.evals.report import write_run_report
from chatcopilot.evals.runner import (
    _load_local_env,
    _run_agent_cases,
    _select_cases,
    run_suite,
)
from chatcopilot.contracts.prompt import BotPromptProfile


@pytest.fixture(autouse=True)
def _isolate_official_eval_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CHATCOPILOT_EVALS_DATA_DIR",
        str(tmp_path / "evals-cache"),
    )
    monkeypatch.setattr(gaia, "_DEFAULT_CACHE_DIR", tmp_path / "gaia-cache")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


@contextmanager
def _test_dir() -> Iterator[Path]:
    root = Path("reports") / "evals" / "test-runs" / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        if root.is_dir():
            shutil.rmtree(root)


def test_select_cases_preserves_suite_order_and_rejects_bad_selection() -> None:
    cases = get_cases("ifeval")
    selected = _select_cases(
        cases,
        case_ids=[cases[2].case_id, cases[0].case_id],
        limit=None,
    )

    assert [case.case_id for case in selected] == [
        cases[0].case_id,
        cases[2].case_id,
    ]
    with pytest.raises(ValueError, match="duplicate"):
        _select_cases(
            cases,
            case_ids=[cases[0].case_id, cases[0].case_id],
            limit=None,
        )
    with pytest.raises(ValueError, match="unknown"):
        _select_cases(cases, case_ids=["missing"], limit=None)
    with pytest.raises(ValueError, match="cannot be used together"):
        _select_cases(
            cases,
            case_ids=[cases[0].case_id],
            limit=1,
        )


def test_dry_run_emits_case_progress_and_checkpoints() -> None:
    cases = get_cases("ifeval")
    events: list[dict] = []
    with _test_dir() as root:
        output = root / "run"
        result = run_suite(
            "ifeval",
            dry_run=True,
            case_ids=[cases[1].case_id, cases[0].case_id],
            output=output,
            progress_callback=events.append,
        )

        assert [case.case_id for case in result.cases] == [
            cases[0].case_id,
            cases[1].case_id,
        ]
        assert [event["event"] for event in events] == [
            "suite_started",
            "case_started",
            "case_completed",
            "case_started",
            "case_completed",
            "suite_completed",
        ]
        payload = json.loads((output / "result.json").read_text(encoding="utf-8"))
        assert len(payload["cases"]) == 2
        assert payload["cases"][0]["started_at"]
        assert payload["cases"][0]["finished_at"]


def test_report_checkpoint_keeps_previous_json_when_replace_fails() -> None:
    with _test_dir() as root:
        result = run_suite(
            "ifeval",
            dry_run=True,
            case_ids=["ifeval-json-format"],
        )
        write_run_report(result, root)
        previous = (root / "result.json").read_text(encoding="utf-8")

        with patch(
            "pathlib.Path.replace",
            side_effect=OSError("replace failed"),
        ):
            with pytest.raises(OSError, match="replace failed"):
                write_run_report(result, root)

        assert (root / "result.json").read_text(encoding="utf-8") == previous
        assert not list(root.glob(".*.tmp"))


def test_catalog_queries_are_generic_and_hide_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "CHATCOPILOT_GAIA_DATA_PATH",
        "CHATCOPILOT_GAIA_SMOKE",
        "CHATCOPILOT_HF_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    profiles = list_profile_descriptors()
    suites = list_suite_descriptors()
    by_id = {item["suite_id"]: item for item in suites}

    assert profiles[0]["profile_id"] == "agent-comparison-mvp"
    assert set(by_id) == {
        "agentstrata-canary-self-update-v1",
        "agentstrata-capabilities-v1",
        "agentstrata-qq-message-flow-v1",
        "gaia",
        "bfcl",
        "ifeval",
        "swe-bench-verified",
        "webarena",
    }
    assert by_id["ifeval"]["ready"] is True
    assert by_id["agentstrata-canary-self-update-v1"]["status"] == "planned"
    assert by_id["agentstrata-canary-self-update-v1"]["ready"] is False
    assert by_id["agentstrata-capabilities-v1"]["case_count"] == 25
    assert by_id["agentstrata-capabilities-v1"]["track"] == "agent"
    assert by_id["agentstrata-qq-message-flow-v1"]["case_count"] == 7
    assert by_id["agentstrata-qq-message-flow-v1"]["track"] == "qq_message_flow"
    assert by_id["agentstrata-capabilities-v1"]["default_preset"] == "quick"
    assert (
        by_id["agentstrata-capabilities-v1"]["capability_status"]
        == "image_generation:not_configured"
    )
    assert by_id["bfcl"]["execution_scope"] == "direct_llm/function_call_protocol"
    assert by_id["swe-bench-verified"]["implemented"] is False
    assert "balanced-100" in by_id["gaia"]["selection_policy"]
    assert "simple/relevance=Lv1" in by_id["bfcl"]["level_policy"]
    assert "instruction family" in by_id["ifeval"]["category_policy"]
    cases = list_case_summaries("bfcl")
    assert cases
    assert "answer" not in json.dumps(cases).lower()
    assert "expected_calls" not in json.dumps(cases)


def test_agent_runtime_is_closed_when_case_execution_fails() -> None:
    case = get_cases("ifeval")[0]
    fake_agent_runtime = MagicMock()
    fake_agent_runtime.new_session.side_effect = RuntimeError("session failed")
    runtime = SimpleNamespace(
        source_path=Path("bots/test/bot.yaml"),
        instance_id="test-bot",
        platform_type="qq",
        agent_backend="native",
        prompt_profile=BotPromptProfile(identity="test", response_style="concise"),
        capability_policies=(),
        skills=(),
        tool_packs=(),
        tool_features=(),
        exclude_tools=(),
        rag_sources=(),
        mcp_servers=(),
        subagents=(),
        spec=SimpleNamespace(llm=SimpleNamespace(env_prefix="CHATCOPILOT_TEST")),
    )
    with (
        _test_dir() as root,
        patch(
            "chatcopilot.evals.runner.resolve_bot_spec_path",
            return_value=Path("bots/test/bot.yaml"),
        ),
        patch(
            "chatcopilot.evals.runner.load_botspec",
            return_value=object(),
        ),
        patch(
            "chatcopilot.evals.runner.assemble_runtime_context",
            return_value=runtime,
        ),
        patch("chatcopilot.evals.runner._load_local_env"),
        patch(
            "chatcopilot.evals.runner.load_config",
            return_value=MagicMock(),
        ),
        patch(
            "chatcopilot.evals.runner.assemble_agent_runtime",
            return_value=fake_agent_runtime,
        ),
    ):
        with pytest.raises(RuntimeError, match="session failed"):
            _run_agent_cases(
                "ifeval",
                (case,),
                bot="bots/test/bot.yaml",
                workspace_root=root / "workspace",
            )

    fake_agent_runtime.close.assert_called_once_with()


def test_agent_runtime_is_closed_when_prompt_plan_session_creation_fails() -> None:
    case = get_cases("ifeval")[0]
    fake_agent_runtime = MagicMock()
    fake_agent_runtime.new_session.side_effect = RuntimeError("prompt failed")
    runtime = SimpleNamespace(
        source_path=Path("bots/test/bot.yaml"),
        instance_id="test-bot",
        platform_type="qq",
        agent_backend="native",
        prompt_profile=BotPromptProfile(identity="test", response_style="concise"),
        capability_policies=(),
        tool_packs=(),
        tool_features=(),
        exclude_tools=(),
        skills=(),
        rag_sources=(),
        mcp_servers=(),
        subagents=(),
        spec=SimpleNamespace(llm=SimpleNamespace(env_prefix="CHATCOPILOT_TEST")),
    )
    with (
        patch(
            "chatcopilot.evals.runner.resolve_bot_spec_path",
            return_value=Path("bots/test/bot.yaml"),
        ),
        patch(
            "chatcopilot.evals.runner.load_botspec",
            return_value=object(),
        ),
        patch(
            "chatcopilot.evals.runner.assemble_runtime_context",
            return_value=runtime,
        ),
        patch("chatcopilot.evals.runner._load_local_env"),
        patch(
            "chatcopilot.evals.runner.load_config",
            return_value=MagicMock(),
        ),
        patch(
            "chatcopilot.evals.runner.assemble_agent_runtime",
            return_value=fake_agent_runtime,
        ),
    ):
        with pytest.raises(RuntimeError, match="prompt failed"):
            _run_agent_cases(
                "ifeval",
                (case,),
                bot="bots/test/bot.yaml",
            )

    fake_agent_runtime.close.assert_called_once_with()


def test_machine_eval_env_overrides_bot_local_and_is_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "CHATCOPILOT_IFEVAL_DATA_PATH"
    monkeypatch.setenv(key, "global-value")

    with temporary_eval_env({key: "bot-value"}):
        assert os.environ[key] == "global-value"

    assert os.environ[key] == "global-value"

    missing_key = "CHATCOPILOT_EVAL_LOCAL_ONLY_FIXTURE"
    monkeypatch.delenv(missing_key, raising=False)
    with temporary_eval_env({missing_key: "bot-value"}):
        assert os.environ[missing_key] == "bot-value"
    assert missing_key not in os.environ


def test_bot_local_snapshot_marker_cannot_bypass_machine_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "CHATCOPILOT_EVALUATION_ENV_SNAPSHOT"
    key = "CHATCOPILOT_EVAL_PRECEDENCE_FIXTURE"
    repository = tmp_path / "repo"
    bot_dir = repository / "bots/example"
    bot_dir.mkdir(parents=True)
    bot_spec = bot_dir / "bot.yaml"
    bot_spec.write_text("id: example\n", encoding="utf-8")
    (bot_dir / "local.env").write_text(
        f"export {marker}=1\nexport {key}=bot-local-value\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(marker, raising=False)
    monkeypatch.setenv(key, "machine-value")

    snapshot = bot_env(
        EvaluationBotRef(instance_id="example", bot_spec=bot_spec),
        repository,
    )

    assert snapshot[key] == "machine-value"
    assert snapshot[marker] == "1"
    with temporary_eval_env({marker: "1", key: "untrusted-value"}):
        assert os.environ[key] == "machine-value"


def test_bot_private_runtime_env_has_identical_preflight_and_worker_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "CHATCOPILOT_GAIA_DATA_PATH"
    monkeypatch.setenv(key, "service-value")
    values = {key: "bot-local-value"}

    with temporary_eval_env(values):
        assert os.environ[key] == "service-value"
        worker = evaluation_subprocess_env(values)
        assert worker[key] == "service-value"

    assert os.environ[key] == "service-value"


def test_windows_eval_paths_are_normalized_for_wsl() -> None:
    values = {
        "CHATCOPILOT_GAIA_DATA_PATH": (r"D:\datasets\gaia\metadata.jsonl"),
        "CHATCOPILOT_GAIA_MANIFEST_PATH": ("reports/evals/manifests/gaia.json"),
    }
    with patch("chatcopilot.evals.env.os.name", "posix"):
        normalized = normalize_eval_env(values)

    assert normalized["CHATCOPILOT_GAIA_DATA_PATH"] == "/mnt/d/datasets/gaia/metadata.jsonl"
    assert normalized["CHATCOPILOT_GAIA_MANIFEST_PATH"] == "reports/evals/manifests/gaia.json"


def test_eval_cli_local_env_uses_same_wsl_path_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "CHATCOPILOT_GAIA_DATA_PATH"
    monkeypatch.delenv(key, raising=False)
    with _test_dir() as root:
        env_file = root / "local.env"
        env_file.write_text(
            f"{key}=" + r"D:\datasets\gaia\metadata.jsonl" + "\n",
            encoding="utf-8",
        )
        with patch("chatcopilot.evals.env.os.name", "posix"):
            _load_local_env(env_file)

    assert os.environ[key] == "/mnt/d/datasets/gaia/metadata.jsonl"
    monkeypatch.delenv(key, raising=False)


def test_gaia_prepare_rejects_zero_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATCOPILOT_GAIA_DATA_PATH", "configured")
    with patch(
        "chatcopilot.evals.adapters.gaia.load_cases",
        return_value=(),
    ):
        with pytest.raises(ValueError, match="no runnable cases"):
            gaia.prepare_data()


def test_suite_descriptor_marks_smoke_data_as_preparable() -> None:
    suites = list_suite_descriptors()
    by_id = {item["suite_id"]: item for item in suites}

    assert by_id["bfcl"]["data_source"] == "builtin_smoke"
    assert by_id["bfcl"]["uses_smoke_data"] is True
    assert by_id["bfcl"]["prepare_available"] is True
    assert by_id["ifeval"]["data_source"] == "builtin_smoke"
    assert by_id["ifeval"]["prepare_available"] is True


def test_official_cache_makes_bfcl_and_ifeval_balanced_100() -> None:
    _write_bfcl_official_cache(bfcl_cache_dir())
    _write_ifeval_official_cache(ifeval_cache_path())

    suites = list_suite_descriptors()
    by_id = {item["suite_id"]: item for item in suites}

    assert by_id["bfcl"]["data_source"] == "official_cache"
    assert by_id["bfcl"]["case_count"] == 100
    assert by_id["bfcl"]["uses_smoke_data"] is False
    assert by_id["ifeval"]["data_source"] == "official_cache"
    assert by_id["ifeval"]["case_count"] == 100


def test_stream_prepare_suite_refreshes_bfcl_cases_from_cache() -> None:
    def fake_prepare(suite_id: str, _values: dict, _repository: Path) -> dict:
        assert suite_id == "bfcl"
        _write_bfcl_official_cache(bfcl_cache_dir())
        return {
            "suite_id": "bfcl",
            "ready": True,
            "path": str(bfcl_cache_dir()),
        }

    with patch(
        "chatcopilot.evals.application.catalog._run_prepare_process",
        side_effect=fake_prepare,
    ):
        lines = list(stream_prepare_suite("bfcl"))

    assert any("case 数量：100" in line for line in lines)
    assert lines[-1] == "__EXIT__ 0"


def _write_bfcl_official_cache(root: Path) -> None:
    categories = {
        "simple": "BFCL_v3_simple.json",
        "multiple": "BFCL_v3_multiple.json",
        "parallel": "BFCL_v3_parallel.json",
        "parallel_multiple": "BFCL_v3_parallel_multiple.json",
        "relevance": "BFCL_v3_irrelevance.json",
    }
    for category, filename in categories.items():
        rows = []
        answers = []
        for index in range(40):
            case_id = f"{category}_{index}"
            rows.append(
                {
                    "id": case_id,
                    "question": [
                        [
                            {
                                "role": "user",
                                "content": (f"Use {category} tool {index}."),
                            }
                        ]
                    ],
                    "function": [
                        {
                            "name": f"{category}_tool",
                            "description": "test",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "integer"}},
                            },
                        }
                    ],
                }
            )
            ground_truth = (
                [] if category == "relevance" else [{f"{category}_tool": {"value": index}}]
            )
            answers.append(
                {
                    "id": case_id,
                    "ground_truth": ground_truth,
                }
            )
        _write_jsonl(root / filename, rows)
        _write_jsonl(root / "possible_answer" / filename, answers)


def _write_ifeval_official_cache(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    specs = {
        "1": (["punctuation:no_comma"], [{}]),
        "2": (
            ["punctuation:no_comma", "keywords:existence"],
            [{}, {"keywords": ["alpha"]}],
        ),
        "3": (
            [
                "punctuation:no_comma",
                "keywords:existence",
                "detectable_format:json_object",
            ],
            [{}, {"keywords": ["alpha"]}, {}],
        ),
    }
    for level, (instruction_ids, kwargs) in specs.items():
        for index in range(40):
            rows.append(
                {
                    "key": f"l{level}-{index}",
                    "prompt": f"Prompt {level}-{index}",
                    "instruction_id_list": instruction_ids,
                    "kwargs": kwargs,
                }
            )
    _write_jsonl(path, rows)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
