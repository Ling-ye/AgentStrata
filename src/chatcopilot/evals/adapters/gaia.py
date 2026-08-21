"""GAIA adapter.

GAIA is an external benchmark: official data is loaded from a local JSON/JSONL
export, while an opt-in smoke subset keeps the runner path testable without
shipping benchmark content in the repository.

Auto-download: when ``CHATCOPILOT_GAIA_DATA_PATH`` is unset and
``CHATCOPILOT_HF_TOKEN`` is available, the adapter automatically downloads
the GAIA dataset from HuggingFace to a local cache directory.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import string
from pathlib import Path
from typing import Any

from chatcopilot.agent.protocol import AgentTask, ResourceRef
from chatcopilot.core.workspace import Workspace
from chatcopilot.evals.env import positive_int_from_env
from chatcopilot.evals.models import EvalCase, JudgeResult
from chatcopilot.evals.selection import balanced_100_cases, normalize_categories, normalize_level

log = logging.getLogger(__name__)

_ENV_DATA_PATH = "CHATCOPILOT_GAIA_DATA_PATH"
_ENV_FILES_DIR = "CHATCOPILOT_GAIA_FILES_DIR"
_ENV_LEVELS = "CHATCOPILOT_GAIA_LEVELS"
_ENV_MANIFEST_PATH = "CHATCOPILOT_GAIA_MANIFEST_PATH"
_ENV_MAX_CASES = "CHATCOPILOT_GAIA_MAX_CASES"
_ENV_CASE_PROFILE = "CHATCOPILOT_GAIA_CASE_PROFILE"
_ENV_SMOKE = "CHATCOPILOT_GAIA_SMOKE"
_ENV_HF_TOKEN = "CHATCOPILOT_HF_TOKEN"

_HF_REPO_ID = "gaia-benchmark/GAIA"
_DEFAULT_CACHE_DIR = (
    Path(os.environ.get("CHATCOPILOT_EVALS_DATA_DIR", "~/.cache/agentstrata/evals"))
    .expanduser()
    / "gaia"
    / "official"
)

_QUESTION_KEYS = ("Question", "question", "prompt", "input")
_ANSWER_KEYS = ("Final answer", "final_answer", "answer", "Answer")
_ID_KEYS = ("task_id", "id", "case_id", "Task ID")
_LEVEL_KEYS = ("Level", "level")
_FILE_KEYS = ("file_name", "filename", "file", "files")
_CATEGORY_KEYS = ("category", "Category", "domain", "Domain", "topic", "Topic", "task_type", "question_type")


def load_cases(
    limit: int | None = None,
    *,
    auto_download: bool = True,
) -> tuple[EvalCase, ...]:
    """Load GAIA cases from official-style data, or opt-in smoke cases.

    When no data path is configured but ``CHATCOPILOT_HF_TOKEN`` is set,
    automatically download the GAIA dataset from HuggingFace.
    """

    data_path = os.environ.get(_ENV_DATA_PATH, "").strip()

    if not data_path:
        data_path = find_cached_data()

    if not data_path and auto_download:
        auto = _try_auto_download()
        if auto:
            data_path = auto

    if data_path:
        cases = _load_external_cases(Path(data_path).expanduser())
        manifest_path = os.environ.get(_ENV_MANIFEST_PATH, "").strip()
        if manifest_path:
            cases = _apply_manifest(cases, Path(manifest_path).expanduser())
        elif not _manual_case_filter_enabled(limit):
            cases = _select_profile(cases, profile=_case_profile_from_env(), seed=20260614)
    elif _truthy(os.environ.get(_ENV_SMOKE, "")):
        cases = _smoke_cases()
    else:
        cases = []

    if not os.environ.get(_ENV_MANIFEST_PATH, "").strip():
        levels = _levels_from_env()
        if levels:
            cases = [case for case in cases if str(case.metadata.get("level", "")).strip() in levels]

    max_cases = positive_int_from_env(_ENV_MAX_CASES)
    if max_cases is not None:
        cases = cases[:max_cases]
    if limit is not None and limit > 0:
        cases = cases[:limit]
    return tuple(cases)


def prepare_data() -> dict[str, Any]:
    """Prepare the official GAIA data set when backend credentials allow it."""

    configured = os.environ.get(_ENV_DATA_PATH, "").strip()
    if configured:
        cases = load_cases(auto_download=False)
        if not cases:
            raise ValueError(
                "GAIA data is configured but no runnable cases remain after filters or manifest."
            )
        return {"ready": bool(cases), "case_count": len(cases), "source": "configured"}

    downloaded = _try_auto_download()
    if not downloaded:
        raise FileNotFoundError(
            "GAIA data is not ready. Configure CHATCOPILOT_GAIA_DATA_PATH "
            "or CHATCOPILOT_HF_TOKEN on the backend."
        )
    cases = load_cases(auto_download=False)
    if not cases:
        raise ValueError("GAIA data was prepared but no runnable cases were found.")
    return {"ready": bool(cases), "case_count": len(cases), "source": "prepared"}


def build_manifest(
    data_path: Path | None = None,
    *,
    profile: str = "budget-50",
    seed: int = 20260614,
    target_cost_rmb: float = 1.0,
) -> dict[str, Any]:
    """Build a deterministic GAIA manifest without embedding official prompts.

    If *data_path* is None, tries auto-download or env-configured path.
    """

    normalized_profile = _normalize_profile(profile)
    if normalized_profile not in {"budget-50", "balanced-100"}:
        raise ValueError(f"unsupported GAIA manifest profile: {profile}")

    if data_path is None:
        env_path = os.environ.get(_ENV_DATA_PATH, "").strip()
        if not env_path:
            env_path = _try_auto_download()
        if not env_path:
            raise FileNotFoundError(
                "GAIA data not found. Set CHATCOPILOT_GAIA_DATA_PATH or CHATCOPILOT_HF_TOKEN."
            )
        data_path = Path(env_path)

    cases = _load_external_cases(data_path.expanduser())
    selected = _select_profile(cases, profile=normalized_profile, seed=seed)
    return {
        "suite_id": f"gaia-{normalized_profile}",
        "seed": seed,
        "profile": normalized_profile,
        "target_cost_rmb": target_cost_rmb,
        "model": "deepseek-v4-pro",
        "selection": {
            "level_1": sum(1 for case in selected if str(case.metadata.get("level")) == "1"),
            "level_2": sum(1 for case in selected if str(case.metadata.get("level")) == "2"),
            "level_3": sum(1 for case in selected if str(case.metadata.get("level")) == "3"),
        },
        "cases": [_manifest_case(case) for case in selected],
    }


def prepare_task(case: EvalCase, workspace: Workspace) -> AgentTask:
    """Create an AgentTask and stage GAIA attachments into the eval workspace."""

    resources = tuple(_stage_resources(case, workspace))
    return AgentTask(
        text=case.input,
        resources=resources,
        turn_context=_case_context(case),
        metadata={"eval_suite": "gaia", "eval_case": case.case_id},
    )


def judge(case: EvalCase, final_text: str) -> JudgeResult:
    """Judge GAIA with deterministic normalized exact match."""

    expected = str(case.metadata.get("answer", "")).strip()
    if not expected:
        return JudgeResult(
            score=0.0,
            max_score=1.0,
            passed=False,
            reasons=("missing GAIA expected answer",),
            missing=("metadata.answer",),
        )

    expected_norm = _normalize_answer(expected)
    candidates = _answer_candidates(final_text)
    passed = any(_normalize_answer(candidate) == expected_norm for candidate in candidates)
    return JudgeResult(
        score=1.0 if passed else 0.0,
        max_score=1.0,
        passed=passed,
        reasons=("normalized exact match",) if passed else ("normalized exact match failed",),
        missing=() if passed else (expected,),
    )


def write_manifest(
    data_path: Path | None,
    output: Path,
    *,
    profile: str = "budget-50",
    seed: int = 20260614,
    target_cost_rmb: float = 1.0,
) -> dict[str, Any]:
    """Build and write a GAIA manifest file."""

    manifest = build_manifest(
        data_path,
        profile=profile,
        seed=seed,
        target_cost_rmb=target_cost_rmb,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _try_auto_download() -> str:
    """Auto-download GAIA from HuggingFace if token is available and data is missing."""

    hf_token = os.environ.get(_ENV_HF_TOKEN, "").strip()
    if not hf_token:
        return ""

    cache_dir = _DEFAULT_CACHE_DIR
    found = _find_metadata_file(cache_dir)
    if found:
        log.info("GAIA data found at %s (auto-cached)", found)
        os.environ[_ENV_DATA_PATH] = str(found)
        os.environ[_ENV_FILES_DIR] = str(found.parent)
        return str(found)

    log.info("Auto-downloading GAIA dataset from HuggingFace to %s ...", cache_dir)
    try:
        _download_gaia_via_api(hf_token, cache_dir)
    except Exception as exc:
        log.warning("GAIA auto-download failed: %s", exc)
        return ""

    found = _find_metadata_file(cache_dir)
    if found:
        os.environ[_ENV_DATA_PATH] = str(found)
        os.environ[_ENV_FILES_DIR] = str(found.parent)
        log.info("GAIA data downloaded: %s", found)
        return str(found)

    log.warning("GAIA download succeeded but no metadata file found in %s", cache_dir)
    return ""


def find_cached_data() -> str:
    found = _find_metadata_file(_DEFAULT_CACHE_DIR)
    return str(found) if found else ""


def _download_gaia_via_api(token: str, target: Path) -> None:
    """Download GAIA validation split using direct HuggingFace API calls.

    ``huggingface_hub.snapshot_download`` has reliability issues on Windows,
    so we use the REST API directly.
    """

    import requests

    headers = {"Authorization": f"Bearer {token}"}
    api_base = f"https://huggingface.co/api/datasets/{_HF_REPO_ID}"
    resolve_base = f"https://huggingface.co/datasets/{_HF_REPO_ID}/resolve/main"

    def _list_files(path: str) -> list[dict]:
        url = f"{api_base}/tree/main/{path}" if path else f"{api_base}/tree/main"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _collect(path: str) -> list[dict]:
        entries = _list_files(path)
        result: list[dict] = []
        for entry in entries:
            if entry.get("type") == "directory":
                result.extend(_collect(entry["path"]))
            elif entry.get("type") == "file":
                result.append(entry)
        return result

    files = _collect("2023/validation")
    log.info("GAIA: %d files to download", len(files))

    for entry in files:
        rfilename = entry["path"]
        expected_size = entry.get("size")
        local_path = target / rfilename
        if local_path.is_file() and expected_size and local_path.stat().st_size == expected_size:
            continue
        local_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{resolve_base}/{rfilename}"
        resp = requests.get(url, headers=headers, timeout=120, stream=True)
        resp.raise_for_status()
        with local_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    log.info("GAIA: download complete (%d files)", len(files))


def _find_metadata_file(base: Path) -> Path | None:
    """Search for the GAIA metadata file in common HuggingFace layouts."""

    for candidate in (
        base / "2023" / "validation" / "metadata.jsonl",
        base / "validation" / "metadata.jsonl",
        base / "metadata.jsonl",
    ):
        if candidate.is_file():
            return candidate

    for parquet in (
        base / "2023" / "validation" / "metadata.parquet",
        base / "validation" / "metadata.parquet",
    ):
        if parquet.is_file():
            converted = _convert_parquet_to_jsonl(parquet)
            if converted:
                return converted

    for candidate in sorted(base.rglob("metadata.jsonl")):
        if candidate.is_file():
            return candidate
    for candidate in sorted(base.rglob("*.jsonl")):
        if candidate.is_file():
            return candidate
    return None


def _convert_parquet_to_jsonl(parquet_path: Path) -> Path | None:
    """Convert a parquet metadata file to JSONL for the adapter."""

    try:
        import pandas as pd

        df = pd.read_parquet(parquet_path)
        jsonl_path = parquet_path.with_suffix(".jsonl")
        df.to_json(jsonl_path, orient="records", lines=True, force_ascii=False)
        log.info("Converted %s → %s (%d rows)", parquet_path.name, jsonl_path.name, len(df))
        return jsonl_path
    except Exception as exc:
        log.warning("Failed to convert parquet: %s", exc)
        return None


def _load_external_cases(path: Path) -> list[EvalCase]:
    data_file = _resolve_data_file(path)
    rows = _read_rows(data_file)
    files_dir = _resolve_files_dir(path, data_file)
    cases: list[EvalCase] = []
    for index, row in enumerate(rows, start=1):
        question = _first_text(row, _QUESTION_KEYS)
        answer = _first_text(row, _ANSWER_KEYS)
        if not question or not answer:
            raise ValueError(f"{data_file}:{index}: GAIA row must include question and final answer")
        raw_id = _first_text(row, _ID_KEYS) or str(index)
        level = normalize_level(_first_text(row, _LEVEL_KEYS))
        files = _files_from_row(row)
        categories = _categories_from_row(row)
        cases.append(
            EvalCase(
                case_id=f"gaia-{_safe_id(raw_id)}",
                input=question,
                category=f"level-{level}" if level else "gaia",
                expected_behavior="Answer the GAIA task with the concise final answer.",
                metadata={
                    "adapter": "gaia",
                    "source": str(data_file),
                    "task_id": raw_id,
                    "level": level,
                    "problem_categories": categories,
                    "answer": answer,
                    "files": files,
                    "files_dir": str(files_dir) if files_dir is not None else "",
                },
            )
        )
    return cases


def _apply_manifest(cases: list[EvalCase], manifest_path: Path) -> list[EvalCase]:
    if not manifest_path.is_file():
        raise FileNotFoundError(f"GAIA manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest.get("cases") if isinstance(manifest, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"GAIA manifest must contain cases list: {manifest_path}")

    by_task_id = {str(case.metadata.get("task_id", "")).strip(): case for case in cases}
    by_case_id = {case.case_id: case for case in cases}
    selected: list[EvalCase] = []
    missing: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = str(row.get("task_id") or row.get("case_id") or "").strip()
        if not raw_id:
            continue
        case = by_task_id.get(raw_id) or by_case_id.get(raw_id) or by_case_id.get(f"gaia-{_safe_id(raw_id)}")
        if case is None:
            missing.append(raw_id)
            continue
        selected.append(case)
    if missing:
        sample = ", ".join(missing[:5])
        raise ValueError(f"GAIA manifest references missing task_id(s): {sample}")
    return selected


def _select_profile(cases: list[EvalCase], *, profile: str, seed: int) -> list[EvalCase]:
    normalized = _normalize_profile(profile)
    if normalized in {"", "full"}:
        return cases
    if normalized == "budget-50":
        return _select_budget_50(cases, seed=seed)
    if normalized == "balanced-100":
        return balanced_100_cases(
            cases,
            level_of=lambda case: normalize_level(case.metadata.get("level")),
            categories_of=_case_categories,
            seed=seed,
            suite_label="GAIA",
        )
    raise ValueError(f"unsupported GAIA case profile: {profile}")


def _select_budget_50(cases: list[EvalCase], *, seed: int) -> list[EvalCase]:
    rng = random.Random(seed)
    profiled = [(case, _profile_case(case)) for case in cases]
    eligible = [
        (case, profile)
        for case, profile in profiled
        if profile["level"] in {"1", "2"} and profile["cost_risk"] != "high"
    ]
    selected = _take_level(eligible, "1", 42, rng) + _take_level(eligible, "2", 8, rng)
    selected_ids = {case.case_id for case in selected}
    if len(selected) < 50:
        fillers = [
            (case, profile)
            for case, profile in eligible
            if case.case_id not in selected_ids
        ]
        rng.shuffle(fillers)
        selected.extend(case for case, _profile in fillers[: 50 - len(selected)])
    if len(selected) < 50:
        raise ValueError(f"GAIA budget-50 requires 50 eligible cases, got {len(selected)}")
    return selected[:50]


def _take_level(
    profiled: list[tuple[EvalCase, dict[str, Any]]],
    level: str,
    count: int,
    rng: random.Random,
) -> list[EvalCase]:
    bucket = [(case, profile) for case, profile in profiled if profile["level"] == level]
    bucket.sort(key=lambda item: _budget_sort_key(item[0], item[1]))
    low = [item for item in bucket if item[1]["cost_risk"] == "low"]
    medium = [item for item in bucket if item[1]["cost_risk"] == "medium"]
    rng.shuffle(low)
    rng.shuffle(medium)
    ordered = low + medium
    return [case for case, _profile in ordered[:count]]


def _budget_sort_key(case: EvalCase, profile: dict[str, Any]) -> tuple[int, int, str]:
    risk_rank = {"low": 0, "medium": 1, "high": 2}.get(str(profile.get("cost_risk")), 3)
    return (risk_rank, len(case.input), str(case.metadata.get("task_id", "")))


def _manifest_case(case: EvalCase) -> dict[str, Any]:
    profile = _profile_case(case)
    return {
        "task_id": str(case.metadata.get("task_id", "")).strip(),
        "level": profile["level"],
        "categories": _case_categories(case),
        "tags": profile["tags"],
        "answer_type": profile["answer_type"],
        "cost_risk": profile["cost_risk"],
    }


def _manual_case_filter_enabled(limit: int | None) -> bool:
    return any(
        (
            limit is not None and limit > 0,
            bool(os.environ.get(_ENV_LEVELS, "").strip()),
            bool(os.environ.get(_ENV_MAX_CASES, "").strip()),
        )
    )


def _case_profile_from_env() -> str:
    return _normalize_profile(os.environ.get(_ENV_CASE_PROFILE, "balanced-100"))


def _normalize_profile(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _case_categories(case: EvalCase) -> tuple[str, ...]:
    explicit = case.metadata.get("problem_categories") or ()
    categories = list(normalize_categories(explicit))
    profile = _profile_case(case)
    categories.append(f"answer:{profile['answer_type']}")
    categories.append(f"cost:{profile['cost_risk']}")
    if profile["has_file"]:
        categories.append("requires:file")
    if profile["likely_search"]:
        categories.append("requires:search")
    return normalize_categories(categories or ("uncategorized",))


def _profile_case(case: EvalCase) -> dict[str, Any]:
    level = str(case.metadata.get("level", "")).strip()
    files = tuple(case.metadata.get("files") or ())
    answer = str(case.metadata.get("answer", "")).strip()
    likely_search = _likely_search(case.input)
    answer_type = _answer_type(answer)
    cost_risk = _cost_risk(case, level=level, files=files, likely_search=likely_search)
    tags = [f"level:{level or 'unknown'}", f"answer:{answer_type}", f"cost:{cost_risk}"]
    if files:
        tags.append("file")
    if likely_search:
        tags.append("search")
    if len(answer.split()) <= 4:
        tags.append("short_answer")
    return {
        "level": level,
        "has_file": bool(files),
        "likely_search": likely_search,
        "answer_type": answer_type,
        "cost_risk": cost_risk,
        "tags": tags,
    }


def _likely_search(prompt: str) -> bool:
    text = prompt.lower()
    markers = (
        "who ",
        "when ",
        "where ",
        "which ",
        "website",
        "source",
        "current",
        "latest",
        "published",
        "released",
        "according to",
        "url",
        "search",
        "查找",
        "官网",
        "来源",
        "最新",
        "发布",
    )
    return any(marker in text for marker in markers)


def _answer_type(answer: str) -> str:
    text = answer.strip()
    if not text:
        return "unknown"
    if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?%?", text):
        return "number"
    if re.search(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", text) or re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
        text.lower(),
    ):
        return "date"
    if "," in text or ";" in text or "\n" in text:
        return "list"
    if len(text.split()) <= 5:
        return "entity"
    return "text"


def _cost_risk(case: EvalCase, *, level: str, files: tuple[str, ...], likely_search: bool) -> str:
    text = case.input.lower()
    if level == "3" or len(files) > 1 or len(case.input) > 800:
        return "high"
    high_markers = ("multiple", "several", "compare", "cross-reference", "all of", "多", "比较")
    if any(marker in text for marker in high_markers):
        return "high"
    if level == "2" or files or likely_search or len(case.input) > 300:
        return "medium"
    return "low"


def _resolve_data_file(path: Path) -> Path:
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"GAIA data path not found: {path}")
    preferred = (
        path / "validation.jsonl",
        path / "test.jsonl",
        path / "train.jsonl",
        path / "data.jsonl",
        path / "validation.json",
        path / "data.json",
    )
    for candidate in preferred:
        if candidate.is_file():
            return candidate
    for candidate in sorted(path.glob("*.jsonl")) + sorted(path.glob("*.json")):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"GAIA data directory has no JSON/JSONL file: {path}")


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError(f"{path}:{line_no}: GAIA JSONL row must be an object")
                rows.append(item)
        return rows

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("rows") or payload.get("examples")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    raise ValueError(f"Unsupported GAIA JSON shape: {path}")


def _resolve_files_dir(data_path: Path, data_file: Path) -> Path | None:
    raw = os.environ.get(_ENV_FILES_DIR, "").strip()
    if raw:
        return Path(raw).expanduser()
    if data_path.is_dir():
        for name in ("files", "attachments"):
            candidate = data_path / name
            if candidate.is_dir():
                return candidate
        return data_path
    for name in ("files", "attachments"):
        candidate = data_file.parent / name
        if candidate.is_dir():
            return candidate
    return data_file.parent


def _files_from_row(row: dict[str, Any]) -> tuple[str, ...]:
    raw = next((row[key] for key in _FILE_KEYS if key in row), None)
    if raw in (None, ""):
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return (str(raw).strip(),)


def _categories_from_row(row: dict[str, Any]) -> tuple[str, ...]:
    raw_values: list[Any] = []
    for key in _CATEGORY_KEYS:
        if key in row:
            raw_values.append(row.get(key))
    annotator = row.get("Annotator Metadata") or row.get("annotator_metadata")
    if isinstance(annotator, dict):
        for key in ("category", "domain", "topic", "task_type", "question_type", "tools", "Tools"):
            if key in annotator:
                raw_values.append(annotator.get(key))
    flattened: list[Any] = []
    for value in raw_values:
        if isinstance(value, (list, tuple, set)):
            flattened.extend(value)
        else:
            flattened.append(value)
    return normalize_categories(flattened)


def _stage_resources(case: EvalCase, workspace: Workspace) -> list[ResourceRef]:
    files = tuple(case.metadata.get("files") or ())
    if not files:
        return []
    files_dir_raw = str(case.metadata.get("files_dir", "")).strip()
    if not files_dir_raw:
        raise FileNotFoundError(f"{case.case_id}: GAIA files_dir is not configured")
    files_dir = Path(files_dir_raw).expanduser()
    case_dir = (workspace.uploads / "gaia" / _safe_id(case.case_id)).resolve()
    case_dir.mkdir(parents=True, exist_ok=True)

    resources: list[ResourceRef] = []
    for raw_name in files:
        source = _resolve_source_file(files_dir, str(raw_name))
        target = (case_dir / source.name).resolve()
        if not workspace.is_inside(target):
            raise ValueError(f"{case.case_id}: staged file escapes workspace: {target}")
        shutil.copy2(source, target)
        resources.append(
            ResourceRef(
                name=target.name,
                path=str(target),
                kind="file",
                schema={
                    "benchmark": "gaia",
                    "case_id": case.case_id,
                    "relative_path": workspace.relpath(target),
                },
            )
        )
    return resources


def _resolve_source_file(files_dir: Path, raw_name: str) -> Path:
    candidate = Path(raw_name).expanduser()
    if not candidate.is_absolute():
        candidate = files_dir / candidate
    files_root = files_dir.resolve()
    source = candidate.resolve()
    try:
        source.relative_to(files_root)
    except ValueError as exc:
        raise ValueError(f"GAIA attachment escapes files_dir: {raw_name}") from exc
    if not source.is_file():
        raise FileNotFoundError(f"GAIA attachment not found: {source}")
    return source


def _case_context(case: EvalCase) -> str:
    parts = [
        "## Eval Case Context",
        f"case_id: {case.case_id}",
        f"category: {case.category}",
        f"expected_behavior: {case.expected_behavior}",
        "For GAIA scoring, provide a concise final answer. Avoid extra explanation unless needed.",
    ]
    level = str(case.metadata.get("level", "")).strip()
    if level:
        parts.append(f"gaia_level: {level}")
    return "\n".join(parts)


def _smoke_cases() -> list[EvalCase]:
    return [
        EvalCase(
            case_id="gaia-smoke-arithmetic",
            input="GAIA smoke task: What is 17 + 25? Reply with only the final answer.",
            category="smoke",
            expected_behavior="Return the concise final answer.",
            metadata={"adapter": "gaia", "source": "builtin-smoke", "level": "1", "answer": "42"},
        ),
        EvalCase(
            case_id="gaia-smoke-normalization",
            input="GAIA smoke task: What color is the clear daytime sky usually? Reply with one word.",
            category="smoke",
            expected_behavior="Return the concise final answer.",
            metadata={"adapter": "gaia", "source": "builtin-smoke", "level": "1", "answer": "blue"},
        ),
    ]


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _answer_candidates(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    candidates = [stripped]
    patterns = (
        r"(?im)^\s*(?:final answer|answer|答案|最终答案)\s*[:：]\s*(.+?)\s*$",
        r"(?im)(?:final answer|answer|答案|最终答案)\s*[:：]\s*(.+?)\s*$",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text):
            candidate = str(match).strip()
            if candidate:
                candidates.append(candidate)
    non_empty_lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if non_empty_lines:
        candidates.append(non_empty_lines[-1])
    return tuple(dict.fromkeys(candidates))


def _normalize_answer(value: str) -> str:
    text = value.strip().lower()
    text = _strip_code_fence(text)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


def _levels_from_env() -> set[str]:
    raw = os.environ.get(_ENV_LEVELS, "").strip()
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip(".-") or "case"


__all__ = [
    "build_manifest",
    "judge",
    "load_cases",
    "prepare_data",
    "prepare_task",
    "write_manifest",
]
