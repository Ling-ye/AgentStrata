"""Read-only catalog and data-preparation helpers for evaluations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

from chatcopilot.evals.application.bots import (
    EvaluationBotRef,
    bot_env,
    evaluation_subprocess_env,
    temporary_eval_env,
)
from chatcopilot.evals.models import EvalCase, to_jsonable
from chatcopilot.evals.official_data import suite_data_status
from chatcopilot.evals.plugins import get_evaluation_plugin
from chatcopilot.evals.profiles import profile_descriptors
from chatcopilot.evals.registry import (
    get_cases,
    get_manifest,
    list_standards,
    normalize_suite_id,
)

def _repo(repository_root: Path | None) -> Path:
    return (repository_root or Path.cwd()).expanduser().resolve()


def _load_suite_cases(
    suite_id: str,
    bot: EvaluationBotRef | None = None,
    *,
    repository_root: Path | None = None,
) -> tuple[EvalCase, ...]:
    values = bot_env(bot, _repo(repository_root)) if bot is not None else {}
    with temporary_eval_env(values):
        return get_cases(suite_id, auto_prepare=False)


def list_profile_descriptors() -> list[dict[str, Any]]:
    return profile_descriptors()


def list_suite_descriptors(
    bot: EvaluationBotRef | None = None,
    *,
    repository_root: Path | None = None,
) -> list[dict[str, Any]]:
    values = bot_env(bot, _repo(repository_root)) if bot is not None else {}
    descriptors: list[dict[str, Any]] = []
    with temporary_eval_env(values):
        for standard in list_standards():
            manifest = get_manifest(standard.suite_id)
            implemented = manifest.status == "implemented"
            cases: tuple[EvalCase, ...] = ()
            error = ""
            if implemented:
                try:
                    plugin = get_evaluation_plugin(manifest.plugin_id)
                    if manifest.driver_id not in plugin.allowed_drivers:
                        raise ValueError(
                            f"plugin {manifest.plugin_id} does not allow {manifest.driver_id}"
                        )
                    cases = get_cases(standard.suite_id, auto_prepare=False)
                except Exception as exc:  # noqa: BLE001
                    error = f"{type(exc).__name__}: {exc}"
            ready = implemented and bool(cases) and not error
            reason = ""
            if not implemented:
                reason = "该评测套件当前仅预留 adapter，尚未实现执行链路。"
            elif error:
                reason = error
            elif not cases:
                reason = standard.setup_hint or "当前没有可运行 case。"
            data_status = suite_data_status(standard.suite_id)
            parameters = [
                {
                    "name": option.name,
                    "type": option.type,
                    "label": option.label,
                    "default": option.default,
                }
                for option in manifest.options
            ]
            descriptors.append(
                {
                    **to_jsonable(standard),
                    "version": manifest.version,
                    "status": manifest.status,
                    "plugin_id": manifest.plugin_id,
                    "driver_id": manifest.driver_id,
                    "driver": manifest.driver_id,
                    "execution_scope": _suite_execution_scope(manifest.suite_id),
                    "capability_status": _suite_capability_status(manifest.suite_id),
                    "default_preset": manifest.default_preset,
                    "presets": [to_jsonable(item) for item in manifest.presets],
                    "implemented": implemented,
                    "ready": ready,
                    "case_count": len(cases),
                    "unavailable_reason": reason,
                    "prepare_available": _suite_prepare_available(
                        manifest.prepare_supported,
                        standard.suite_id,
                        data_status,
                    ),
                    "parameters": parameters,
                    "selection_policy": _suite_selection_policy(standard.suite_id),
                    "level_policy": _suite_level_policy(standard.suite_id),
                    "category_policy": _suite_category_policy(standard.suite_id),
                    "data_source": data_status.get("source", ""),
                    "data_cache_path": data_status.get("cache_path", ""),
                    "uses_smoke_data": bool(data_status.get("uses_smoke", False)),
                }
            )
    return descriptors


def _suite_execution_scope(suite_id: str) -> str:
    if suite_id == "bfcl":
        return "direct_llm/function_call_protocol"
    if suite_id == "agentstrata-capabilities-v1":
        return "product_agent_mixed_drivers"
    return "agent_runtime" if suite_id in {"gaia", "ifeval"} else "unavailable"


def _suite_capability_status(suite_id: str) -> str:
    if suite_id == "agentstrata-capabilities-v1":
        return "image_generation:not_configured"
    return "configured"


def _suite_prepare_available(
    prepare_supported: bool,
    suite_id: str,
    data_status: dict[str, Any],
) -> bool:
    if not prepare_supported:
        return False
    if suite_id in {"bfcl", "ifeval"}:
        return data_status.get("source") in {"builtin_smoke", "unavailable"}
    if suite_id == "gaia":
        return data_status.get("source") not in {"official_cache", "configured"} and bool(
            os.environ.get("CHATCOPILOT_HF_TOKEN", "").strip()
        )
    return False


def _suite_selection_policy(suite_id: str) -> str:
    if suite_id in {"gaia", "bfcl", "ifeval"}:
        return (
            "外部数据默认 balanced-100：总计 100 题，优先按 Level 1/2/3 选择 34/33/33；"
            "若官方数据某 Level 不足则全取该 Level 并从其他 Level 补足；"
            "显式 limit、MAX_CASES、GAIA manifest/levels 或 BFCL category 时不自动采样。"
        )
    return ""


def _suite_level_policy(suite_id: str) -> str:
    if suite_id == "gaia":
        return "使用官方 Level 字段。"
    if suite_id == "bfcl":
        return (
            "按调用复杂度映射：simple/relevance=Lv1，multiple=Lv2，parallel/parallel_multiple=Lv3。"
        )
    if suite_id == "ifeval":
        return "按指令复杂度映射：1 条指令=Lv1，2 条=Lv2，3 条及以上=Lv3。"
    return ""


def _suite_category_policy(suite_id: str) -> str:
    if suite_id == "gaia":
        return (
            "优先覆盖数据行 category/domain/topic/task_type/question_type；"
            "缺失时使用附件、搜索倾向、答案类型和成本标签。"
        )
    if suite_id == "bfcl":
        return (
            "覆盖 BFCL 官方类别及调用形态：simple、multiple、parallel、"
            "parallel_multiple、relevance。"
        )
    if suite_id == "ifeval":
        return (
            "覆盖 instruction family，例如 punctuation、keywords、"
            "detectable_format、length_constraints。"
        )
    return ""


def list_case_summaries(
    suite_id: str,
    bot: EvaluationBotRef | None = None,
    *,
    repository_root: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        _case_summary(case)
        for case in _load_suite_cases(
            suite_id,
            bot,
            repository_root=repository_root,
        )
    ]


def get_case_descriptor(
    suite_id: str,
    case_id: str,
    bot: EvaluationBotRef | None = None,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    for case in _load_suite_cases(
        suite_id,
        bot,
        repository_root=repository_root,
    ):
        if case.case_id == case_id:
            return {
                **_case_summary(case),
                "input": case.input,
                "context": case.context,
                "rubric": case.rubric,
                "expected_behavior": case.expected_behavior,
                "metadata": _safe_case_metadata(case),
            }
    raise KeyError(case_id)


def _case_summary(case: EvalCase) -> dict[str, Any]:
    text = " ".join(case.input.split())
    files = case.metadata.get("files") if isinstance(case.metadata, dict) else ()
    return {
        "case_id": case.case_id,
        "category": case.category,
        "summary": text[:180] + ("…" if len(text) > 180 else ""),
        "has_attachments": bool(files),
        "attachment_count": (len(files) if isinstance(files, (list, tuple)) else 0),
        "source": _safe_source(case.metadata.get("source", "")),
    }


def _safe_case_metadata(case: EvalCase) -> dict[str, Any]:
    allowed = {
        "adapter",
        "source",
        "bfcl_category",
        "level",
        "task_id",
        "files",
        "problem_categories",
    }
    result = {key: to_jsonable(value) for key, value in case.metadata.items() if key in allowed}
    if "source" in result:
        result["source"] = _safe_source(result["source"])
    return result


def _safe_source(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or "/" in text or "\\" in text:
        return "external"
    return text


def stream_prepare_suite(
    suite_id: str,
    bot: EvaluationBotRef | None = None,
    *,
    repository_root: Path | None = None,
) -> Iterator[str]:
    normalized = normalize_suite_id(suite_id)
    manifest = get_manifest(normalized)
    if manifest.status != "implemented" or not manifest.prepare_supported:
        raise ValueError(f"{normalized} does not support backend data preparation")
    yield f"[evals] 开始准备 {normalized.upper()} 官方数据。"
    repository = _repo(repository_root)
    values = bot_env(bot, repository) if bot is not None else {}
    result = _run_prepare_process(normalized, values, repository)
    with temporary_eval_env(values):
        cases = get_cases(normalized, auto_prepare=False)
    path = result.get("path") or result.get("source") or result.get("cache_path") or ""
    if path:
        yield f"[evals] 官方数据缓存：{path}"
    yield f"[evals] {normalized.upper()} 数据已就绪，case 数量：{len(cases)}。"
    yield "__EXIT__ 0"


def _run_prepare_process(
    suite_id: str,
    values: dict[str, str],
    repository_root: Path,
) -> dict[str, Any]:
    """Prepare remote data in a child process with an immutable env snapshot."""

    environment = evaluation_subprocess_env(values)
    source_root = str(repository_root / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            source_root,
            *[item for item in environment.get("PYTHONPATH", "").split(os.pathsep) if item],
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "chatcopilot.evals",
            "prepare",
            "--suite",
            suite_id,
            "--json",
        ],
        cwd=repository_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{suite_id} data preparation subprocess failed with exit code {completed.returncode}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{suite_id} data preparation returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("ready") is not True:
        raise RuntimeError(f"{suite_id} data preparation did not become ready")
    return {str(key): value for key, value in payload.items()}


__all__ = [
    "get_case_descriptor",
    "list_case_summaries",
    "list_profile_descriptors",
    "list_suite_descriptors",
    "stream_prepare_suite",
]
