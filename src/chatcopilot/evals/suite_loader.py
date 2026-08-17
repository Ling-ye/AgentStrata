"""Compatibility loader for legacy Profile YAML and strict manifest Cases."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chatcopilot.evals.models import EvalCase

if TYPE_CHECKING:
    from chatcopilot.evals.models import SuiteManifest

_SUITES_DIR = "suites"
_REQUIRED_CASE_FIELDS = ("case_id", "input", "category", "expected_behavior")


def load_suite_cases(
    suite_id: str,
    *,
    manifest: SuiteManifest | None = None,
) -> tuple[EvalCase, ...]:
    """Load strict manifest Cases or the retained versioned Profile format."""

    if manifest is not None:
        from chatcopilot.evals.plugins.base import CaseLoadContext
        from chatcopilot.evals.plugins.generic_agent import load_declarative_cases

        if manifest.suite_id != suite_id.strip().lower().replace("_", "-"):
            raise ValueError("manifest suite_id does not match requested suite")
        return load_declarative_cases(
            CaseLoadContext(manifest=manifest, auto_prepare=False)
        )

    suite_dir = _suite_dir(suite_id)
    cases_data = _load_yaml_resource(suite_dir / "cases.yaml", field="cases")
    raw_cases = cases_data.get("cases")
    if raw_cases is None:
        return ()
    if not isinstance(raw_cases, list):
        raise ValueError(f"{suite_id}/cases.yaml: cases 必须是 list")
    return tuple(
        _parse_legacy_case(suite_id, index, raw)
        for index, raw in enumerate(raw_cases, start=1)
    )


def _suite_dir(suite_id: str) -> Path:
    normalized = suite_id.strip().lower().replace("_", "-")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts or "\\" in normalized:
        raise ValueError("suite id escapes the packaged suites root")
    return Path(_SUITES_DIR) / path


def _load_yaml_resource(relative_path: Path, *, field: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML 依赖，请先安装：python -m pip install PyYAML") from exc
    resource = resources.files("chatcopilot.evals").joinpath(*relative_path.parts)
    if not resource.is_file():
        return {field: []}
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{relative_path.as_posix()}: 顶层必须是 mapping")
    return data


def _parse_legacy_case(suite_id: str, index: int, raw: Any) -> EvalCase:
    if not isinstance(raw, dict):
        raise ValueError(f"{suite_id}/cases.yaml: cases[{index}] 必须是 mapping")
    for field in _REQUIRED_CASE_FIELDS:
        if not str(raw.get(field, "")).strip():
            raise ValueError(f"{suite_id}/cases.yaml: cases[{index}].{field} 不能为空")
    return EvalCase(
        case_id=str(raw["case_id"]).strip(),
        input=str(raw["input"]).strip(),
        category=str(raw["category"]).strip(),
        expected_behavior=str(raw["expected_behavior"]).strip(),
        must_have=_str_list(raw.get("must_have"), suite_id, index, "must_have"),
        must_not=_str_list(raw.get("must_not"), suite_id, index, "must_not"),
        context=str(raw.get("context", "") or "").strip(),
        rubric=str(raw.get("rubric", "") or "").strip(),
        metadata=_metadata(raw.get("metadata"), suite_id, index),
    )


def _str_list(raw: Any, suite_id: str, index: int, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{suite_id}/cases.yaml: cases[{index}].{field} 必须是 list")
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _metadata(raw: Any, suite_id: str, index: int) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{suite_id}/cases.yaml: cases[{index}].metadata 必须是 mapping")
    return dict(raw)


__all__ = ["load_suite_cases"]
