"""Read-only catalog and data-preparation helpers for evaluations."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from chatcopilot.evals.env import normalize_eval_env
from chatcopilot.evals.models import EvalCase, to_jsonable
from chatcopilot.evals.official_data import prepare_official_data, suite_data_status
from chatcopilot.evals.profiles import profile_descriptors
from chatcopilot.evals.registry import get_cases, list_standards, normalize_suite_id
from console.control.discovery import repo_root
from console.control.instances import BotInstance

IMPLEMENTED_SUITES = {"gaia", "bfcl", "ifeval"}
_ENV_LOCK = threading.RLock()
_EVAL_ENV_KEYS = {
    "CHATCOPILOT_GAIA_DATA_PATH",
    "CHATCOPILOT_GAIA_FILES_DIR",
    "CHATCOPILOT_GAIA_LEVELS",
    "CHATCOPILOT_GAIA_MANIFEST_PATH",
    "CHATCOPILOT_GAIA_MAX_CASES",
    "CHATCOPILOT_GAIA_CASE_PROFILE",
    "CHATCOPILOT_GAIA_SMOKE",
    "CHATCOPILOT_BFCL_DATA_DIR",
    "CHATCOPILOT_BFCL_MAX_CASES",
    "CHATCOPILOT_BFCL_CATEGORY",
    "CHATCOPILOT_BFCL_CASE_PROFILE",
    "CHATCOPILOT_IFEVAL_DATA_PATH",
    "CHATCOPILOT_IFEVAL_MAX_CASES",
    "CHATCOPILOT_IFEVAL_CASE_PROFILE",
    "CHATCOPILOT_EVALS_DATA_DIR",
    "CHATCOPILOT_HF_TOKEN",
}


def _load_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _bot_spec_path(instance: BotInstance) -> Path:
    raw = Path(instance.bot_spec)
    path = raw if raw.is_absolute() else repo_root() / raw
    resolved = path.resolve()
    resolved.relative_to(repo_root().resolve())
    if not resolved.is_file():
        raise FileNotFoundError(f"BotSpec not found: {instance.instance_id}")
    return resolved


def _bot_env(instance: BotInstance) -> dict[str, str]:
    return normalize_eval_env(
        _load_env_values(_bot_spec_path(instance).parent / "local.env")
    )


@contextmanager
def _temporary_env(values: dict[str, str]) -> Iterator[None]:
    """Apply bot-owned eval data settings for one serialized inspection."""

    with _ENV_LOCK:
        old = {key: os.environ.get(key) for key in _EVAL_ENV_KEYS}
        for key, value in values.items():
            if key in _EVAL_ENV_KEYS:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _load_suite_cases(
    suite_id: str,
    instance: BotInstance | None = None,
) -> tuple[EvalCase, ...]:
    values = _bot_env(instance) if instance is not None else {}
    with _temporary_env(values):
        return get_cases(suite_id, auto_prepare=False)


def list_profile_descriptors() -> list[dict[str, Any]]:
    return profile_descriptors()


def list_suite_descriptors(
    instance: BotInstance | None = None,
) -> list[dict[str, Any]]:
    values = _bot_env(instance) if instance is not None else {}
    descriptors: list[dict[str, Any]] = []
    with _temporary_env(values):
        for standard in list_standards():
            implemented = standard.suite_id in IMPLEMENTED_SUITES
            cases: tuple[EvalCase, ...] = ()
            error = ""
            if implemented:
                try:
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
                    "name": "dry_run",
                    "type": "boolean",
                    "label": "仅校验，不调用模型",
                    "default": False,
                }
            ]
            if standard.suite_id == "gaia":
                parameters.append(
                    {
                        "name": "llm_judge",
                        "type": "boolean",
                        "label": "启用 LLM Judge",
                        "default": True,
                    }
                )
            descriptors.append(
                {
                    **to_jsonable(standard),
                    "implemented": implemented,
                    "ready": ready,
                    "case_count": len(cases),
                    "unavailable_reason": reason,
                    "prepare_available": _suite_prepare_available(
                        standard.suite_id,
                        data_status,
                    ),
                    "parameters": parameters,
                    "selection_policy": _suite_selection_policy(
                        standard.suite_id
                    ),
                    "level_policy": _suite_level_policy(standard.suite_id),
                    "category_policy": _suite_category_policy(
                        standard.suite_id
                    ),
                    "data_source": data_status.get("source", ""),
                    "data_cache_path": data_status.get("cache_path", ""),
                    "uses_smoke_data": bool(
                        data_status.get("uses_smoke", False)
                    ),
                }
            )
    return descriptors


def _suite_prepare_available(
    suite_id: str,
    data_status: dict[str, Any],
) -> bool:
    if suite_id in {"bfcl", "ifeval"}:
        return data_status.get("source") in {"builtin_smoke", "unavailable"}
    if suite_id == "gaia":
        return (
            data_status.get("source") not in {"official_cache", "configured"}
            and bool(os.environ.get("CHATCOPILOT_HF_TOKEN", "").strip())
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
            "按调用复杂度映射：simple/relevance=Lv1，multiple=Lv2，"
            "parallel/parallel_multiple=Lv3。"
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
    instance: BotInstance | None = None,
) -> list[dict[str, Any]]:
    return [
        _case_summary(case)
        for case in _load_suite_cases(suite_id, instance)
    ]


def get_case_descriptor(
    suite_id: str,
    case_id: str,
    instance: BotInstance | None = None,
) -> dict[str, Any]:
    for case in _load_suite_cases(suite_id, instance):
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
    files = (
        case.metadata.get("files")
        if isinstance(case.metadata, dict)
        else ()
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "summary": text[:180] + ("…" if len(text) > 180 else ""),
        "has_attachments": bool(files),
        "attachment_count": (
            len(files) if isinstance(files, (list, tuple)) else 0
        ),
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
    result = {
        key: to_jsonable(value)
        for key, value in case.metadata.items()
        if key in allowed
    }
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
    instance: BotInstance | None = None,
) -> Iterator[str]:
    normalized = normalize_suite_id(suite_id)
    if normalized not in IMPLEMENTED_SUITES:
        raise ValueError(
            f"{normalized} does not support backend data preparation"
        )
    yield f"[evals] 开始准备 {normalized.upper()} 官方数据。"
    values = _bot_env(instance) if instance is not None else {}
    with _temporary_env(values):
        result = prepare_official_data(normalized)
        cases = get_cases(normalized, auto_prepare=False)
    path = (
        result.get("path")
        or result.get("source")
        or result.get("cache_path")
        or ""
    )
    if path:
        yield f"[evals] 官方数据缓存：{path}"
    yield f"[evals] {normalized.upper()} 数据已就绪，case 数量：{len(cases)}。"
    yield "__EXIT__ 0"


__all__ = [
    "get_case_descriptor",
    "list_case_summaries",
    "list_profile_descriptors",
    "list_suite_descriptors",
    "stream_prepare_suite",
]
