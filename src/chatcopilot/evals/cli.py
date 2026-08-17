"""Command line interface for canonical AgentStrata Evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from chatcopilot.core.logging import configure_logging
from chatcopilot.evals.adapters import gaia
from chatcopilot.evals.advisor import advise_capability_evaluation
from chatcopilot.evals.evaluations import (
    EvaluationValidationError,
    evaluation_result_to_dict,
    run_evaluation,
    validate_evaluation,
)
from chatcopilot.evals.official_data import prepare_official_data
from chatcopilot.evals.paths import is_managed_evaluation_output
from chatcopilot.evals.registry import get_manifest, get_standard, list_standards
from chatcopilot.evals.report import compare_reports, render_compare_markdown


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    _configure_eval_logging()
    parser = argparse.ArgumentParser(
        prog="python -m chatcopilot evals",
        description="Run and inspect AgentStrata Evaluations.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List available evaluation Suites.")

    describe = sub.add_parser("describe", help="Describe one evaluation Suite.")
    describe.add_argument("--suite", required=True)

    run = sub.add_parser("run", help="Run one Profile comparison or Suite Evaluation.")
    source = run.add_mutually_exclusive_group()
    source.add_argument(
        "--request",
        help="Complete Evaluation request as a JSON object or JSON file path.",
    )
    source.add_argument("--profile", help="Versioned comparison Profile id.")
    source.add_argument("--suite", help="Benchmark Suite id.")
    run.add_argument("--evaluation-id")
    run.add_argument("--bot", help="Bot id or path to bot.yaml.")
    run.add_argument(
        "--preset",
        help=(
            "Comparison preset (quick/standard/custom) or a Suite-defined "
            "preset such as quick/full/security/qq-live/custom."
        ),
    )
    run.add_argument("--target", action="append", dest="targets")
    run.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Profile Case ref or exact Suite Case id; repeatable.",
    )
    run.add_argument("--repetitions", type=int)
    run.add_argument("--max-wall-seconds", type=float)
    run.add_argument("--seed", type=int)
    run.add_argument(
        "--option",
        action="append",
        dest="options",
        metavar="NAME=JSON",
        help="Suite option; repeatable. Values use JSON syntax.",
    )
    run.add_argument(
        "--confirm-external-write",
        action="store_true",
        help="Confirm this one manual run may perform the Suite's bounded external writes.",
    )
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--llm-judge", action="store_true")
    run.add_argument(
        "--output",
        type=Path,
        help="Standalone Evaluation directory outside the managed service root.",
    )
    run.add_argument("--validate-only", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--json", action="store_true")

    gaia_manifest = sub.add_parser("gaia-manifest", help="Build a deterministic GAIA manifest.")
    gaia_manifest.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Official GAIA JSON/JSONL path (auto-download if omitted and HF token set).",
    )
    gaia_manifest.add_argument("--output", type=Path, required=True)
    gaia_manifest.add_argument("--profile", default="budget-50")
    gaia_manifest.add_argument("--seed", type=int, default=20260614)
    gaia_manifest.add_argument("--target-cost-rmb", type=float, default=1.0)
    gaia_manifest.add_argument("--json", action="store_true")

    compare = sub.add_parser("compare", help="Compare two Suite reports.")
    compare.add_argument("--base", type=Path, required=True)
    compare.add_argument("--new", type=Path, required=True)
    compare.add_argument("--json", action="store_true")

    prepare = sub.add_parser("prepare", help="Prepare official Suite data.")
    prepare.add_argument("--suite", choices=("gaia", "bfcl", "ifeval"), required=True)
    prepare.add_argument("--json", action="store_true")

    advise = sub.add_parser(
        "advise",
        help="Suggest capability Cases for changed paths without starting an Evaluation.",
    )
    advise.add_argument(
        "--changed-path",
        action="append",
        dest="changed_paths",
        required=True,
        help="Repository-relative changed path; repeatable.",
    )
    advise.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "list":
        return _cmd_list()
    if args.command == "describe":
        return _cmd_describe(args.suite)
    if args.command == "run":
        return _cmd_run(args, parser)
    if args.command == "gaia-manifest":
        return _cmd_gaia_manifest(args)
    if args.command == "compare":
        return _cmd_compare(args)
    if args.command == "prepare":
        return _cmd_prepare(args)
    if args.command == "advise":
        return _cmd_advise(args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _cmd_advise(args: argparse.Namespace) -> int:
    try:
        advice = advise_capability_evaluation(args.changed_paths)
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "code": "invalid_changed_paths",
                    "message": str(exc),
                    "checks": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    payload = {
        "changed_paths": list(advice.changed_paths),
        "categories": list(advice.categories),
        "reason": advice.reason,
        "case_ids": list(advice.case_ids),
        "recommended_preset": advice.recommended_preset,
        "manual_only": True,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"recommended preset: {advice.recommended_preset}\n"
            f"cases: {', '.join(advice.case_ids)}\n"
            f"reason: {advice.reason}\n"
            "No Evaluation was started."
        )
    return 0


def _cmd_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        request = (
            _load_request_argument(args.request)
            if args.request
            else _request_from_args(args, parser)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "code": "invalid_evaluation_request",
                    "message": str(exc),
                    "checks": [],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    if "evaluation_id" not in request and args.output is not None:
        request = {**request, "evaluation_id": args.output.name}
    validation = validate_evaluation(request)
    if args.validate_only:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 0 if validation["ready"] else 2
    if not validation["ready"]:
        failed = [item for item in validation["checks"] if not item.get("ok")]
        payload = {
            "code": str(validation.get("code") or "evaluation_validation_failed"),
            "message": "; ".join(str(item.get("detail", "")) for item in failed),
            "checks": validation["checks"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    effective = validation["effective_request"]
    if "evaluation_id" not in request:
        request = {**request, "evaluation_id": effective["evaluation_id"]}
    output = args.output
    if output is None:
        print(
            json.dumps(
                {
                    "code": "evaluation_output_required",
                    "message": (
                        "standalone evals run requires --output outside the "
                        "managed Evaluation service root"
                    ),
                    "checks": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    if output.name != effective["evaluation_id"]:
        print(
            json.dumps(
                {
                    "code": "evaluation_output_mismatch",
                    "message": (
                        "output directory basename must equal evaluation_id: "
                        f"{output.name!r} != {effective['evaluation_id']!r}"
                    ),
                    "checks": [],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if is_managed_evaluation_output(output):
        print(
            json.dumps(
                {
                    "code": "evaluation_output_reserved",
                    "message": (
                        "standalone evals run cannot write inside the managed "
                        "Evaluation service root"
                    ),
                    "checks": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    def progress(payload: dict[str, Any]) -> None:
        print(
            "__EVAL_EVENT__ " + json.dumps(payload, ensure_ascii=False),
            file=sys.stderr if args.json else sys.stdout,
            flush=True,
        )

    try:
        result = run_evaluation(
            request,
            output=output,
            progress_callback=progress,
            resume=args.resume,
        )
    except EvaluationValidationError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "code": (
                        "evaluation_resume_rejected" if args.resume else "evaluation_run_failed"
                    ),
                    "message": str(exc),
                    "checks": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    payload = evaluation_result_to_dict(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_result_summary_line(result))
        print(f"report: {output}")
    return _result_exit_code(result)


def _is_product_capability_result(result: Any) -> bool:
    summary = result.summary if isinstance(result.summary, dict) else {}
    return bool(
        result.kind == "suite"
        and (
            result.suite == "agentstrata-capabilities-v1"
            or summary.get("score_scope")
            == "product capability gates are not averaged into an intelligence score"
        )
    )


def _result_summary_line(result: Any) -> str:
    summary = result.summary if isinstance(result.summary, dict) else {}
    prefix = (
        f"{result.evaluation_id}: kind={result.kind} status={result.status} "
        f"trials={len(result.trials)}"
    )
    if _is_product_capability_result(result):
        return (
            f"{prefix} verdict={summary.get('verdict', 'error/indeterminate')} "
            f"passed={int(summary.get('passed', 0))} "
            f"failed={int(summary.get('failed', 0))} "
            f"critical_violations={int(summary.get('critical_violations', 0))} "
            f"infrastructure_errors={int(summary.get('infrastructure_errors', 0))}"
        )
    return f"{prefix} score_ratio={float(summary.get('score_ratio', 0)):.3f}"


def _result_exit_code(result: Any) -> int:
    if result.status in {"interrupted", "cancelled"}:
        return 130
    if result.status != "completed":
        return 1
    if _is_product_capability_result(result):
        summary = result.summary if isinstance(result.summary, dict) else {}
        return 0 if summary.get("verdict") == "passed" else 1
    return 0


def _load_request_argument(raw: str) -> dict[str, Any]:
    value = str(raw).strip()
    if not value:
        raise ValueError("--request cannot be empty")
    if value.startswith("{"):
        payload = json.loads(value)
    else:
        payload = json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--request must resolve to a JSON object")
    nested = payload.get("core_request")
    if isinstance(nested, dict):
        return {str(key): item for key, item in nested.items()}

    # The managed application owns these descriptive fields. They remain
    # deliberately outside the strict Core request schema.
    kind = str(payload.get("kind") or "").strip()
    core: dict[str, Any] = {
        "kind": kind,
        "bot": str(
            payload.get("bot") or payload.get("bot_spec") or payload.get("bot_id") or ""
        ).strip(),
    }
    if payload.get("evaluation_id"):
        core["evaluation_id"] = payload["evaluation_id"]
    if kind == "comparison":
        core.update(
            {
                "profile": str(payload.get("profile") or payload.get("profile_id") or ""),
                "preset": str(payload.get("preset") or "quick"),
            }
        )
        if core["preset"] == "custom":
            target_field = "target_ids" if "target_ids" in payload else "targets"
            core["targets"] = payload.get(target_field)
            core["case_refs"] = payload.get("case_refs")
            core["repetitions"] = payload.get("repetitions")
            core["max_wall_seconds"] = payload.get("max_wall_seconds")
            core["seed"] = payload.get("seed")
        return core
    if kind == "suite":
        core.update(
            {
                "suite": str(payload.get("suite") or payload.get("suite_id") or ""),
                "preset": str(payload.get("preset") or ""),
                "repetitions": payload.get("repetitions", 1),
                "max_wall_seconds": payload.get("max_wall_seconds", 0),
                "seed": payload.get("seed", 0),
                "options": payload.get("options", {}),
                "confirm_external_write": payload.get(
                    "confirm_external_write",
                    False,
                ),
                "dry_run": payload.get("dry_run", False),
                "llm_judge": payload.get("llm_judge", False),
            }
        )
        if "case_ids" in payload:
            core["case_ids"] = payload["case_ids"]
        return core
    raise ValueError("request kind must be comparison or suite")


def _request_from_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> dict[str, Any]:
    if not args.profile and not args.suite:
        parser.error("evals run requires --request, --profile, or --suite")
    if args.profile:
        request: dict[str, Any] = {
            "kind": "comparison",
            "bot": args.bot or "",
            "profile": args.profile,
            "preset": args.preset or "quick",
        }
        if args.evaluation_id:
            request["evaluation_id"] = args.evaluation_id
        if args.targets is not None:
            request["targets"] = args.targets
        if args.case_ids is not None:
            request["case_refs"] = args.case_ids
        if args.repetitions is not None:
            request["repetitions"] = args.repetitions
        if args.max_wall_seconds is not None:
            request["max_wall_seconds"] = args.max_wall_seconds
        if args.seed is not None:
            request["seed"] = args.seed
        if args.dry_run or args.llm_judge:
            raise ValueError("--dry-run and --llm-judge apply only to Suite Evaluations")
        if args.options or args.confirm_external_write:
            raise ValueError("--option and --confirm-external-write apply only to Suites")
        return request

    request = {
        "kind": "suite",
        "bot": args.bot or "",
        "suite": args.suite,
        "preset": args.preset or "",
        "repetitions": args.repetitions if args.repetitions is not None else 1,
        "max_wall_seconds": (args.max_wall_seconds if args.max_wall_seconds is not None else 0),
        "seed": args.seed if args.seed is not None else 0,
        "options": _parse_suite_options(args.options),
        "confirm_external_write": bool(args.confirm_external_write),
        "dry_run": bool(args.dry_run),
        "llm_judge": bool(args.llm_judge),
    }
    if args.evaluation_id:
        request["evaluation_id"] = args.evaluation_id
    if args.case_ids is not None:
        request["case_ids"] = args.case_ids
    if args.targets is not None:
        raise ValueError("--target applies only to comparison Evaluations")
    return request


def _parse_suite_options(values: list[str] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw in values or ():
        key, separator, encoded = str(raw).partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError("--option must use NAME=JSON")
        if key in result:
            raise ValueError(f"duplicate Suite option: {key}")
        try:
            result[key] = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON for Suite option {key}") from exc
    return result


def _cmd_gaia_manifest(args: argparse.Namespace) -> int:
    manifest = gaia.write_manifest(
        args.data,
        args.output,
        profile=args.profile,
        seed=args.seed,
        target_cost_rmb=args.target_cost_rmb,
    )
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        selection = manifest.get("selection", {})
        print(
            f"gaia-manifest: output={args.output} "
            f"cases={len(manifest.get('cases', []))} "
            f"level_1={selection.get('level_1', 0)} "
            f"level_2={selection.get('level_2', 0)} "
            f"level_3={selection.get('level_3', 0)}"
        )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    payload = compare_reports(args.base, args.new)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_compare_markdown(payload))
    return 0 if not payload.get("regressions") else 1


def _cmd_prepare(args: argparse.Namespace) -> int:
    payload = prepare_official_data(args.suite)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"{payload.get('suite_id', args.suite)}: "
            f"ready={bool(payload.get('ready', False))} "
            f"path={payload.get('path') or payload.get('data_path') or ''}"
        )
    return 0 if payload.get("ready") else 1


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def _configure_eval_logging() -> None:
    configure_logging("ERROR", "CHATCOPILOT_EVALS_LOG_LEVEL")


def _cmd_list() -> int:
    for standard in list_standards():
        manifest = get_manifest(standard.suite_id)
        availability = (
            "planned/unavailable"
            if manifest.status == "planned"
            else "external-data"
            if standard.requires_external_data
            else "built-in"
        )
        print(f"{standard.suite_id}\t{standard.kind}\t{availability}\t{standard.name}")
    return 0


def _cmd_describe(suite_id: str) -> int:
    standard = get_standard(suite_id)
    manifest = get_manifest(suite_id)
    print(f"id: {standard.suite_id}")
    print(f"name: {standard.name}")
    print(f"kind: {standard.kind}")
    print(f"status: {manifest.status}")
    if manifest.status == "planned":
        print("availability: unavailable")
    print(f"value: {standard.value}")
    print(f"recommendation: {standard.recommendation}")
    print(f"cadence: {standard.cadence}")
    print(f"requires_external_data: {standard.requires_external_data}")
    if standard.official_url:
        print(f"official_url: {standard.official_url}")
    if standard.setup_hint:
        print(f"setup_hint: {standard.setup_hint}")
    return 0


__all__ = ["main"]
