from __future__ import annotations

import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

import pytest

from chatcopilot.agent.context.task_framing import frame_task_message
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.evals.adapters import bfcl, gaia, ifeval
from chatcopilot.evals.cli import main as evals_cli_main
from chatcopilot.evals.models import EvalCase
from chatcopilot.evals.models import EvalCaseResult
from chatcopilot.evals.registry import get_cases, get_standard, list_standards
from chatcopilot.evals.report import compare_reports
from chatcopilot.evals.report import render_summary_markdown
from chatcopilot.evals.runner import _load_local_env
from chatcopilot.evals.runner import _summarize
from chatcopilot.evals.runner import run_suite
from chatcopilot.evals.suite_loader import _parse_legacy_case


@pytest.fixture(autouse=True)
def _isolate_official_eval_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATCOPILOT_EVALS_DATA_DIR", str(tmp_path / "evals-cache"))
    monkeypatch.setattr(gaia, "_DEFAULT_CACHE_DIR", tmp_path / "gaia-cache")


class EvalRegistryTests(unittest.TestCase):
    def test_standard_benchmarks_are_registered(self) -> None:
        suite_ids = {standard.suite_id for standard in list_standards()}

        self.assertEqual(
            {
                "agentstrata-canary-self-update-v1",
                "agentstrata-capabilities-v1",
                "agentstrata-qq-message-flow-v1",
                "gaia",
                "bfcl",
                "ifeval",
                "swe-bench-verified",
                "webarena",
            },
            suite_ids,
        )

    def test_external_standard_describes_setup_instead_of_fake_cases(self) -> None:
        with _patched_env(
            CHATCOPILOT_GAIA_DATA_PATH=None,
            CHATCOPILOT_GAIA_FILES_DIR=None,
            CHATCOPILOT_GAIA_LEVELS=None,
            CHATCOPILOT_GAIA_MANIFEST_PATH=None,
            CHATCOPILOT_GAIA_MAX_CASES=None,
            CHATCOPILOT_GAIA_SMOKE=None,
            CHATCOPILOT_HF_TOKEN=None,
        ):
            standard = get_standard("gaia")

            self.assertTrue(standard.requires_external_data)
            self.assertEqual(get_cases("gaia"), ())
            self.assertIn("CHATCOPILOT_GAIA_DATA_PATH", standard.setup_hint)

    def test_cli_marks_planned_suites_unavailable(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            exit_code = evals_cli_main(["list"])

        self.assertEqual(exit_code, 0)
        lines = output.getvalue().splitlines()
        self.assertIn(
            "agentstrata-canary-self-update-v1\tproduct\tplanned/unavailable\t"
            "AgentStrata Canary 自更新 v1",
            lines,
        )
        self.assertIn(
            "swe-bench-verified\tcode\tplanned/unavailable\tSWE-bench Verified",
            lines,
        )

    def test_cli_describe_exposes_planned_status(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            exit_code = evals_cli_main(["describe", "--suite", "agentstrata-canary-self-update-v1"])

        self.assertEqual(exit_code, 0)
        rendered = output.getvalue()
        self.assertIn("status: planned\n", rendered)
        self.assertIn("availability: unavailable\n", rendered)

    def test_ifeval_has_builtin_smoke_cases(self) -> None:
        standard = get_standard("ifeval")
        cases = get_cases("ifeval")

        self.assertFalse(standard.requires_external_data)
        self.assertGreaterEqual(len(cases), 5)
        self.assertEqual(cases[0].metadata["adapter"], "ifeval")
        self.assertTrue(cases[0].metadata["instruction_checks"])

    def test_yaml_loader_rejects_non_list_rule_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "must_have 必须是 list"):
            _parse_legacy_case(
                "test-suite",
                1,
                {
                    "case_id": "bad-case",
                    "category": "bad",
                    "input": "hello",
                    "expected_behavior": "world",
                    "must_have": "not-a-list",
                },
            )


class EvalRunnerTests(unittest.TestCase):
    def test_ifeval_judge_scores_supported_checks(self) -> None:
        case = next(item for item in get_cases("ifeval") if item.case_id == "ifeval-json-format")

        passed = ifeval.judge(case, '{"name":"baseline","value":1}')
        failed = ifeval.judge(case, "name: baseline")

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)

    def test_load_local_env_accepts_export_syntax(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text("export CHATCOPILOT_TEST_API_KEY='secret'\n", encoding="utf-8")
            old = os.environ.get("CHATCOPILOT_TEST_API_KEY")
            os.environ.pop("CHATCOPILOT_TEST_API_KEY", None)
            try:
                _load_local_env(path)

                self.assertEqual(os.environ.get("CHATCOPILOT_TEST_API_KEY"), "secret")
            finally:
                if old is None:
                    os.environ.pop("CHATCOPILOT_TEST_API_KEY", None)
                else:
                    os.environ["CHATCOPILOT_TEST_API_KEY"] = old

    def test_dry_run_writes_report_without_llm(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"

            result = run_suite("ifeval", dry_run=True, output=output, limit=2)

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.summary["total"], 2)
            self.assertTrue((output / "result.json").is_file())
            self.assertTrue((output / "summary.md").is_file())

    def test_external_suite_reports_unavailable_without_dataset(self) -> None:
        with _patched_env(
            CHATCOPILOT_GAIA_DATA_PATH=None,
            CHATCOPILOT_GAIA_FILES_DIR=None,
            CHATCOPILOT_GAIA_LEVELS=None,
            CHATCOPILOT_GAIA_MANIFEST_PATH=None,
            CHATCOPILOT_GAIA_MAX_CASES=None,
            CHATCOPILOT_GAIA_SMOKE=None,
            CHATCOPILOT_HF_TOKEN=None,
        ):
            result = run_suite("gaia", dry_run=True)

        self.assertEqual(result.status, "unavailable")
        self.assertIn("外部官方数据集", result.error)

    def test_gaia_smoke_cases_are_opt_in(self) -> None:
        with _patched_env(
            CHATCOPILOT_GAIA_DATA_PATH=None,
            CHATCOPILOT_GAIA_MANIFEST_PATH=None,
            CHATCOPILOT_GAIA_SMOKE="1",
            CHATCOPILOT_HF_TOKEN=None,
        ):
            cases = get_cases("gaia")

        self.assertGreaterEqual(len(cases), 2)
        self.assertEqual(cases[0].metadata["adapter"], "gaia")
        self.assertEqual(cases[0].metadata["source"], "builtin-smoke")

    def test_gaia_loads_official_jsonl_and_filters_levels(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "validation.jsonl"
            data.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "task_id": "abc/1",
                                "Question": "What is attached?",
                                "Final answer": "report",
                                "Level": 1,
                                "file_name": "report.txt",
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "task_id": "abc/2",
                                "Question": "What is 2+2?",
                                "Final answer": "4",
                                "Level": 2,
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with _patched_env(
                CHATCOPILOT_GAIA_DATA_PATH=str(data),
                CHATCOPILOT_GAIA_LEVELS="1",
                CHATCOPILOT_GAIA_MANIFEST_PATH=None,
                CHATCOPILOT_GAIA_MAX_CASES=None,
                CHATCOPILOT_GAIA_SMOKE=None,
            ):
                cases = get_cases("gaia")

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].case_id, "gaia-abc-1")
        self.assertEqual(cases[0].metadata["answer"], "report")
        self.assertEqual(cases[0].metadata["files"], ("report.txt",))

    def test_gaia_manifest_filters_and_preserves_order(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "validation.jsonl"
            _write_gaia_rows(
                data,
                [
                    _gaia_row("task-a", level=1, question="What is A?", answer="A"),
                    _gaia_row("task-b", level=1, question="What is B?", answer="B"),
                    _gaia_row("task-c", level=2, question="What is C?", answer="C"),
                ],
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "suite_id": "gaia-budget-50",
                        "cases": [{"task_id": "task-b"}, {"task_id": "task-a"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with _patched_env(
                CHATCOPILOT_GAIA_DATA_PATH=str(data),
                CHATCOPILOT_GAIA_MANIFEST_PATH=str(manifest),
                CHATCOPILOT_GAIA_LEVELS="2",
                CHATCOPILOT_GAIA_MAX_CASES=None,
                CHATCOPILOT_GAIA_SMOKE=None,
            ):
                cases = get_cases("gaia")

        self.assertEqual([case.metadata["task_id"] for case in cases], ["task-b", "task-a"])

    def test_gaia_budget_50_manifest_sampler_distribution(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "validation.jsonl"
            rows = [
                _gaia_row(
                    f"l1-{index}", level=1, question=f"What is item {index}?", answer=str(index)
                )
                for index in range(60)
            ]
            rows.extend(
                _gaia_row(
                    f"l2-{index}",
                    level=2,
                    question=f"Who is subject {index}?",
                    answer=f"Subject {index}",
                )
                for index in range(12)
            )
            rows.extend(
                _gaia_row(
                    f"l3-{index}", level=3, question=f"Compare several sources {index}", answer="x"
                )
                for index in range(5)
            )
            _write_gaia_rows(data, rows)

            manifest = gaia.build_manifest(data, profile="budget-50", seed=7)

        self.assertEqual(len(manifest["cases"]), 50)
        self.assertEqual(manifest["selection"], {"level_1": 42, "level_2": 8, "level_3": 0})
        self.assertTrue(all(item["cost_risk"] in {"low", "medium"} for item in manifest["cases"]))

    def test_gaia_balanced_100_manifest_sampler_distribution_and_categories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "validation.jsonl"
            rows = []
            for level in (1, 2, 3):
                rows.extend(
                    _gaia_row(
                        f"l{level}-{index}",
                        level=level,
                        question=f"What is item {level}-{index}?",
                        answer=str(index),
                        category=f"domain-{index % 4}",
                    )
                    for index in range(40)
                )
            _write_gaia_rows(data, rows)

            manifest = gaia.build_manifest(data, profile="balanced-100", seed=7)

        self.assertEqual(len(manifest["cases"]), 100)
        self.assertEqual(manifest["selection"], {"level_1": 34, "level_2": 33, "level_3": 33})
        for level in ("1", "2", "3"):
            categories = {
                category
                for item in manifest["cases"]
                if item["level"] == level
                for category in item["categories"]
            }
            self.assertTrue({"domain 0", "domain 1", "domain 2", "domain 3"}.issubset(categories))

    def test_gaia_manifest_cli_writes_budget_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "validation.jsonl"
            rows = [
                _gaia_row(
                    f"l1-{index}", level=1, question=f"What is item {index}?", answer=str(index)
                )
                for index in range(60)
            ]
            rows.extend(
                _gaia_row(
                    f"l2-{index}",
                    level=2,
                    question=f"Who is subject {index}?",
                    answer=f"Subject {index}",
                )
                for index in range(12)
            )
            _write_gaia_rows(data, rows)
            output = root / "gaia-budget-50.json"

            exit_code = evals_cli_main(
                [
                    "gaia-manifest",
                    "--data",
                    str(data),
                    "--output",
                    str(output),
                    "--profile",
                    "budget-50",
                    "--seed",
                    "7",
                ]
            )

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["suite_id"], "gaia-budget-50")
        self.assertEqual(len(payload["cases"]), 50)

    def test_gaia_prepare_task_stages_attachment_resource(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = root / "files"
            files.mkdir()
            (files / "report.txt").write_text("hello", encoding="utf-8")
            item = EvalCase(
                case_id="gaia-file-case",
                input="Read the attachment.",
                category="level-1",
                expected_behavior="Answer from the file.",
                metadata={
                    "adapter": "gaia",
                    "answer": "hello",
                    "files": ("report.txt",),
                    "files_dir": str(files),
                },
            )
            workspace = Workspace(
                root=root / "workspace",
                chat_kind="p2p",
                chat_id="eval:gaia",
                user_id="eval-user",
            ).ensure()

            task = gaia.prepare_task(item, workspace)

        self.assertEqual(len(task.resources), 1)
        self.assertEqual(task.resources[0].name, "report.txt")
        self.assertIn("uploads", task.resources[0].path)
        self.assertIn("[本轮资源]", frame_task_message(task))

    def test_gaia_judge_uses_normalized_exact_match(self) -> None:
        case = EvalCase(
            case_id="gaia-judge",
            input="Question",
            category="level-1",
            expected_behavior="Answer exactly.",
            metadata={"adapter": "gaia", "answer": "The Blue Whale"},
        )

        passed = gaia.judge(case, "Final answer: blue whale.")
        failed = gaia.judge(case, "Final answer: humpback whale")

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)

    def test_summary_includes_deepseek_v4_pro_cost_estimate(self) -> None:
        summary = _summarize(
            (
                EvalCaseResult(
                    case_id="cost",
                    suite_id="gaia",
                    status="passed",
                    score=1.0,
                    metadata={
                        "usage_totals": {
                            "prompt_tokens": 1000,
                            "cached_tokens": 200,
                            "completion_tokens": 100,
                        }
                    },
                ),
            )
        )

        cost = summary["cost_estimates"]["deepseek_v4_pro_rmb"]
        self.assertEqual(cost["uncached_tokens"], 800)
        self.assertEqual(cost["cached_tokens"], 200)
        self.assertEqual(cost["completion_tokens"], 100)
        self.assertGreater(cost["estimated_rmb"], 0)

    def test_ifeval_dry_run_uses_builtin_cases(self) -> None:
        result = run_suite("ifeval", dry_run=True, limit=2)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.summary["total"], 2)

    def test_ifeval_balanced_100_sampler_uses_complexity_levels_and_categories(self) -> None:
        cases = []
        check_sets = {
            "1": [{"id": "punctuation:no_comma", "kwargs": {}}],
            "2": [
                {"id": "punctuation:no_comma", "kwargs": {}},
                {"id": "keywords:existence", "kwargs": {"keywords": ["x"]}},
            ],
            "3": [
                {"id": "punctuation:no_comma", "kwargs": {}},
                {"id": "keywords:existence", "kwargs": {"keywords": ["x"]}},
                {"id": "detectable_format:json_object", "kwargs": {}},
            ],
        }
        for level, checks in check_sets.items():
            for index in range(40):
                cases.append(
                    EvalCase(
                        case_id=f"ifeval-l{level}-{index}",
                        input="prompt",
                        category="instruction_following",
                        expected_behavior="follow",
                        metadata={
                            "adapter": "ifeval",
                            "level": level,
                            "instruction_checks": checks,
                            "problem_categories": ("format", f"group-{index % 3}"),
                        },
                    )
                )

        selected = ifeval._select_profile(cases, profile="balanced-100", seed=7)

        self.assertEqual(len(selected), 100)
        self.assertEqual(
            {
                level: sum(1 for case in selected if case.metadata["level"] == level)
                for level in ("1", "2", "3")
            },
            {"1": 34, "2": 33, "3": 33},
        )
        categories = {category for case in selected for category in ifeval._case_categories(case)}
        self.assertTrue({"group 0", "group 1", "group 2"}.issubset(categories))

    def test_compare_reports_detects_regression(self) -> None:
        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            new_dir = Path(tmp) / "new"
            trial = {
                "trial_id": "ifeval-json-format-a1-target",
                "evaluation_id": "eval-base",
                "kind": "suite",
                "suite_id": "ifeval",
                "case_ref": "ifeval:ifeval-json-format",
                "case_id": "ifeval-json-format",
                "target_id": "configured",
                "attempt": 1,
                "outcome": "passed",
                "score": 1.0,
                "max_score": 1.0,
            }
            base_payload = {
                "evaluation_id": "eval-base",
                "kind": "suite",
                "suite": "ifeval",
                "status": "completed",
                "targets": [
                    {
                        "target_id": "configured",
                        "executor": "agent_configured",
                        "backend": "native",
                        "fingerprint": "same-target-fingerprint",
                    }
                ],
                "selected_cases": ["ifeval:ifeval-json-format"],
                "config_snapshot": {
                    "case_hash": "same-case-hash",
                    "judge": "suite-or-profile-defined",
                    "target_fingerprints": {
                        "configured": "same-target-fingerprint",
                    },
                    "definition_fingerprint": "same-definition-fingerprint",
                    "private_runtime_configuration": {},
                },
                "trials": [trial],
                "summary": {"score_ratio": 1.0},
            }
            new_payload = {
                **base_payload,
                "evaluation_id": "eval-new",
                "trials": [{**trial, "outcome": "failed", "score": 0.0}],
                "summary": {"score_ratio": 0.0},
            }
            _write_payload(base_dir, base_payload)
            _write_payload(new_dir, new_payload)

            diff = compare_reports(base_dir, new_dir)

            self.assertLess(diff["score_delta"], 0)
            self.assertEqual(
                diff["regressions"][0]["case_ref"],
                "ifeval:ifeval-json-format",
            )

    def test_compare_reports_rejects_incomparable_evaluations(self) -> None:
        base_payload = {
            "evaluation_id": "eval-base",
            "kind": "suite",
            "suite": "ifeval",
            "profile": "",
            "status": "completed",
            "targets": [
                {
                    "target_id": "configured",
                    "executor": "agent_configured",
                    "backend": "native",
                }
            ],
            "selected_cases": ["ifeval:ifeval-json-format"],
            "config_snapshot": {
                "case_hash": "same-case-hash",
                "judge": "suite-or-profile-defined",
                "target_fingerprints": {
                    "configured": "same-target-fingerprint",
                },
                "definition_fingerprint": "same-definition-fingerprint",
                "private_runtime_configuration": {},
            },
            "trials": [],
            "summary": {"score_ratio": 0.0},
        }
        incompatible = (
            (
                "kind",
                {
                    **base_payload,
                    "kind": "comparison",
                    "suite": "",
                    "profile": "agent-comparison-mvp",
                },
            ),
            ("suites", {**base_payload, "suite": "bfcl"}),
            (
                "Targets",
                {
                    **base_payload,
                    "targets": [
                        {
                            "target_id": "other-target",
                            "executor": "agent_configured",
                            "backend": "native",
                        }
                    ],
                },
            ),
        )
        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            _write_payload(base_dir, base_payload)
            for index, (message, payload) in enumerate(incompatible):
                new_dir = Path(tmp) / f"new-{index}"
                _write_payload(new_dir, payload)
                with self.assertRaisesRegex(ValueError, message):
                    compare_reports(base_dir, new_dir)

    def test_compare_reports_rejects_scope_and_sample_drift(self) -> None:
        trial = {
            "trial_id": "ifeval-json-format-a1-target",
            "evaluation_id": "eval-base",
            "kind": "suite",
            "suite_id": "ifeval",
            "case_ref": "ifeval:ifeval-json-format",
            "case_id": "ifeval-json-format",
            "target_id": "configured",
            "attempt": 1,
            "outcome": "passed",
            "score": 1.0,
            "max_score": 1.0,
        }
        base_payload = {
            "evaluation_id": "eval-base",
            "kind": "suite",
            "suite": "ifeval",
            "status": "completed",
            "targets": [
                {
                    "target_id": "configured",
                    "executor": "agent_configured",
                    "backend": "native",
                }
            ],
            "selected_cases": ["ifeval:ifeval-json-format"],
            "config_snapshot": {
                "case_hash": "same-case-hash",
                "judge": "suite-or-profile-defined",
                "target_fingerprints": {
                    "configured": "same-target-fingerprint",
                },
                "definition_fingerprint": "same-definition-fingerprint",
                "private_runtime_configuration": {},
            },
            "trials": [trial],
            "summary": {"score_ratio": 1.0},
        }

        variants: list[tuple[str, dict]] = []
        partial = json.loads(json.dumps(base_payload))
        partial["status"] = "partial"
        variants.append(("completed", partial))
        selected_cases = json.loads(json.dumps(base_payload))
        selected_cases["selected_cases"] = ["ifeval:other-case"]
        variants.append(("selected_cases", selected_cases))
        case_hash = json.loads(json.dumps(base_payload))
        case_hash["config_snapshot"]["case_hash"] = "changed-case-hash"
        variants.append(("case_hash", case_hash))
        judge = json.loads(json.dumps(base_payload))
        judge["config_snapshot"]["judge"] = "changed-judge"
        variants.append(("judge", judge))
        backend = json.loads(json.dumps(base_payload))
        backend["targets"][0]["backend"] = "codex"
        variants.append(("Targets", backend))
        sample = json.loads(json.dumps(base_payload))
        sample["trials"][0]["attempt"] = 2
        variants.append(("sample keys", sample))

        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            _write_payload(base_dir, base_payload)
            for index, (message, payload) in enumerate(variants):
                new_dir = Path(tmp) / f"new-{index}"
                _write_payload(new_dir, payload)
                with self.assertRaisesRegex(ValueError, message):
                    compare_reports(base_dir, new_dir)

    def test_compare_reports_rejects_different_profiles(self) -> None:
        base_payload = {
            "evaluation_id": "eval-base",
            "kind": "comparison",
            "profile": "agent-comparison-mvp",
            "suite": "",
            "status": "completed",
            "targets": [
                {
                    "target_id": "codex",
                    "executor": "agent_isolated",
                    "backend": "codex",
                },
                {
                    "target_id": "native",
                    "executor": "agent_isolated",
                    "backend": "native",
                },
            ],
            "selected_cases": ["ifeval:ifeval-json-format"],
            "config_snapshot": {
                "case_hash": "same-case-hash",
                "judge": "suite-or-profile-defined",
            },
            "trials": [],
            "summary": {},
        }
        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            new_dir = Path(tmp) / "new"
            _write_payload(base_dir, base_payload)
            _write_payload(
                new_dir,
                {**base_payload, "profile": "another-profile"},
            )

            with self.assertRaisesRegex(ValueError, "profiles"):
                compare_reports(base_dir, new_dir)

    def test_compare_reports_rejects_non_finite_scores(self) -> None:
        payload = {
            "evaluation_id": "eval-base",
            "kind": "suite",
            "suite": "ifeval",
            "status": "completed",
            "targets": [
                {
                    "target_id": "configured",
                    "executor": "agent_configured",
                    "backend": "native",
                }
            ],
            "selected_cases": ["ifeval:ifeval-json-format"],
            "config_snapshot": {
                "case_hash": "same-case-hash",
                "judge": "suite-or-profile-defined",
            },
            "trials": [],
            "summary": {"score_ratio": float("nan")},
        }
        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            new_dir = Path(tmp) / "new"
            _write_payload(base_dir, payload)
            _write_payload(new_dir, {**payload, "evaluation_id": "eval-new"})

            with self.assertRaisesRegex(ValueError, "finite"):
                compare_reports(base_dir, new_dir)

    def test_summary_markdown_renders_cost_fields(self) -> None:
        result = run_suite("ifeval", dry_run=True, limit=1)
        payload = _as_json(result)
        payload["summary"]["cost_estimates"] = {
            "deepseek_v4_pro_rmb": {
                "estimated_rmb": 0.123456,
                "estimated_rmb_per_case": 0.123456,
                "prompt_tokens": 10,
                "cached_tokens": 1,
                "completion_tokens": 2,
            }
        }
        from chatcopilot.evals.models import EvalRunResult

        synthetic = EvalRunResult(
            suite_id=result.suite_id,
            bot=result.bot,
            status=result.status,
            started_at=result.started_at,
            duration_seconds=result.duration_seconds,
            cases=result.cases,
            summary=payload["summary"],
        )

        markdown = render_summary_markdown(synthetic)

        self.assertIn("deepseek_v4_pro_estimated_rmb", markdown)


class BFCLAdapterTests(unittest.TestCase):
    def test_smoke_cases_load_without_external_data(self) -> None:
        with _patched_env(CHATCOPILOT_BFCL_DATA_DIR=None):
            cases = bfcl.load_cases()

        self.assertEqual(len(cases), 5)
        categories = {c.metadata["bfcl_category"] for c in cases}
        self.assertTrue(categories.issuperset({"simple", "multiple", "parallel", "relevance"}))

    def test_smoke_cases_filter_by_category(self) -> None:
        with _patched_env(CHATCOPILOT_BFCL_DATA_DIR=None):
            cases = bfcl.load_cases(category="simple")

        self.assertTrue(all(c.metadata["bfcl_category"] == "simple" for c in cases))
        self.assertGreaterEqual(len(cases), 1)

    def test_judge_simple_correct(self) -> None:
        case = bfcl.load_cases()[0]
        expected = case.metadata["expected_calls"]
        result = bfcl.judge(case, expected)

        self.assertTrue(result.passed)
        self.assertEqual(result.score, 1.0)

    def test_judge_simple_wrong_function(self) -> None:
        case = bfcl.load_cases()[0]
        wrong = [{"name": "wrong_function", "arguments": {"x": 1}}]
        result = bfcl.judge(case, wrong)

        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)

    def test_judge_relevance_no_calls_passes(self) -> None:
        relevance_cases = [
            c for c in bfcl.load_cases() if c.metadata["bfcl_category"] == "relevance"
        ]
        self.assertTrue(len(relevance_cases) > 0)
        result = bfcl.judge(relevance_cases[0], [])

        self.assertTrue(result.passed)

    def test_judge_relevance_with_calls_fails(self) -> None:
        relevance_cases = [
            c for c in bfcl.load_cases() if c.metadata["bfcl_category"] == "relevance"
        ]
        result = bfcl.judge(
            relevance_cases[0], [{"name": "get_stock_price", "arguments": {"ticker": "AAPL"}}]
        )

        self.assertFalse(result.passed)

    def test_judge_parallel_order_independent(self) -> None:
        parallel_cases = [c for c in bfcl.load_cases() if c.metadata["bfcl_category"] == "parallel"]
        self.assertTrue(len(parallel_cases) > 0)
        case = parallel_cases[0]
        expected = case.metadata["expected_calls"]
        reversed_calls = list(reversed(expected))

        result = bfcl.judge(case, reversed_calls)
        self.assertTrue(result.passed)

    def test_build_tools_schema_returns_openai_format(self) -> None:
        case = bfcl.load_cases()[0]
        tools = bfcl.build_tools_schema(case)

        self.assertTrue(len(tools) > 0)
        self.assertEqual(tools[0]["type"], "function")
        self.assertIn("name", tools[0]["function"])

    def test_build_messages_returns_user_message(self) -> None:
        case = bfcl.load_cases()[0]
        messages = bfcl.build_messages(case)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertTrue(len(messages[0]["content"]) > 0)

    def test_bfcl_dry_run(self) -> None:
        result = run_suite("bfcl", dry_run=True, limit=3)

        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.summary["total"], 3)

    def test_bfcl_balanced_100_sampler_uses_levels_and_categories(self) -> None:
        cases = []
        category_by_level = {
            "1": ("simple", "relevance"),
            "2": ("multiple",),
            "3": ("parallel", "parallel_multiple"),
        }
        for level, categories in category_by_level.items():
            for index in range(40):
                category = categories[index % len(categories)]
                cases.append(
                    EvalCase(
                        case_id=f"bfcl-l{level}-{index}",
                        input="prompt",
                        category=f"bfcl-{category}",
                        expected_behavior="call",
                        metadata={
                            "adapter": "bfcl",
                            "level": level,
                            "bfcl_category": category,
                            "problem_categories": (category, f"group-{index % 3}"),
                        },
                    )
                )

        selected = bfcl._select_profile(cases, profile="balanced-100", seed=7)

        self.assertEqual(len(selected), 100)
        self.assertEqual(
            {
                level: sum(1 for case in selected if case.metadata["level"] == level)
                for level in ("1", "2", "3")
            },
            {"1": 34, "2": 33, "3": 33},
        )
        categories = {category for case in selected for category in bfcl._case_categories(case)}
        self.assertTrue(
            {"simple", "relevance", "multiple", "parallel", "parallel multiple"}.issubset(
                categories
            )
        )


class LLMJudgeTests(unittest.TestCase):
    def test_parse_verdict_valid_json(self) -> None:
        from chatcopilot.evals.judges_llm import _parse_verdict

        result = _parse_verdict('{"match": true, "reasoning": "semantically equivalent"}')
        self.assertTrue(result["match"])
        self.assertEqual(result["reasoning"], "semantically equivalent")

    def test_parse_verdict_code_fenced_json(self) -> None:
        from chatcopilot.evals.judges_llm import _parse_verdict

        text = '```json\n{"match": false, "reasoning": "different answer"}\n```'
        result = _parse_verdict(text)
        self.assertFalse(result["match"])

    def test_parse_verdict_partial_match(self) -> None:
        from chatcopilot.evals.judges_llm import _parse_verdict

        result = _parse_verdict('some text "match": true more text')
        self.assertTrue(result["match"])

    def test_parse_verdict_garbage_input(self) -> None:
        from chatcopilot.evals.judges_llm import _parse_verdict

        result = _parse_verdict("I cannot evaluate this")
        self.assertFalse(result["match"])

    def test_judge_llm_rubric_no_expected_answer(self) -> None:
        from chatcopilot.evals.judges_llm import judge_llm_rubric
        from unittest.mock import MagicMock

        case = EvalCase(
            case_id="test",
            input="Question",
            category="test",
            expected_behavior="Answer",
            metadata={"adapter": "gaia"},
        )
        config = MagicMock()
        result = judge_llm_rubric(case, "some answer", config)

        self.assertFalse(result.passed)
        self.assertIn("no expected answer", result.reasons[0])


def _as_json(result):
    from chatcopilot.evals.models import to_jsonable

    return to_jsonable(result)


def _write_payload(path: Path, payload: dict) -> None:
    path.mkdir(parents=True)
    (path / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _gaia_row(
    task_id: str,
    *,
    level: int,
    question: str,
    answer: str,
    category: str | None = None,
) -> dict:
    row = {
        "task_id": task_id,
        "Question": question,
        "Final answer": answer,
        "Level": level,
    }
    if category:
        row["category"] = category
    return row


def _write_gaia_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


@contextmanager
def _patched_env(**values: str | None) -> Iterator[None]:
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
