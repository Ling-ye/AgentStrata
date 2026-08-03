"""Official benchmark data preparation for eval adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ENV_CACHE_ROOT = "CHATCOPILOT_EVALS_DATA_DIR"
_BFCL_REPO_BASE = "https://huggingface.co/datasets/gorilla-llm/Berkeley-Function-Calling-Leaderboard/resolve/main"
_IFEVAL_URL = (
    "https://raw.githubusercontent.com/google-research/google-research/master/"
    "instruction_following_eval/data/input_data.jsonl"
)

_BFCL_DATA_FILES = (
    "BFCL_v3_simple.json",
    "BFCL_v3_multiple.json",
    "BFCL_v3_parallel.json",
    "BFCL_v3_parallel_multiple.json",
    "BFCL_v3_irrelevance.json",
)
_BFCL_ANSWER_FILES = tuple(
    f"possible_answer/{name}"
    for name in _BFCL_DATA_FILES
    if name != "BFCL_v3_irrelevance.json"
)


def default_cache_root() -> Path:
    raw = os.environ.get(_ENV_CACHE_ROOT, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "agentstrata" / "evals"


def bfcl_cache_dir() -> Path:
    return default_cache_root() / "bfcl" / "official"


def ifeval_cache_path() -> Path:
    return default_cache_root() / "ifeval" / "official" / "input_data.jsonl"


def has_bfcl_official_data(path: Path | None = None) -> bool:
    root = path or bfcl_cache_dir()
    return all((root / name).is_file() and (root / name).stat().st_size > 0 for name in _BFCL_DATA_FILES) and all(
        (root / name).is_file() and (root / name).stat().st_size > 0 for name in _BFCL_ANSWER_FILES
    )


def has_ifeval_official_data(path: Path | None = None) -> bool:
    target = path or ifeval_cache_path()
    return target.is_file() and target.stat().st_size > 0


def prepare_bfcl_official_data() -> dict[str, Any]:
    target = bfcl_cache_dir()
    for relative in (*_BFCL_DATA_FILES, *_BFCL_ANSWER_FILES):
        _download_file(f"{_BFCL_REPO_BASE}/{relative}", target / relative)
    if not has_bfcl_official_data(target):
        raise FileNotFoundError(f"BFCL official data was not fully prepared: {target}")
    return {"suite_id": "bfcl", "ready": True, "path": str(target)}


def prepare_ifeval_official_data() -> dict[str, Any]:
    target = ifeval_cache_path()
    _download_file(_IFEVAL_URL, target)
    if not has_ifeval_official_data(target):
        raise FileNotFoundError(f"IFEval official data was not prepared: {target}")
    return {"suite_id": "ifeval", "ready": True, "path": str(target)}


def prepare_gaia_official_data() -> dict[str, Any]:
    from chatcopilot.evals.adapters import gaia

    return {"suite_id": "gaia", **gaia.prepare_data()}


def prepare_official_data(suite_id: str) -> dict[str, Any]:
    normalized = suite_id.strip().lower().replace("_", "-")
    if normalized == "gaia":
        return prepare_gaia_official_data()
    if normalized == "bfcl":
        return prepare_bfcl_official_data()
    if normalized == "ifeval":
        return prepare_ifeval_official_data()
    raise ValueError(f"{normalized} does not support official data preparation")


def suite_data_status(suite_id: str) -> dict[str, Any]:
    normalized = suite_id.strip().lower().replace("_", "-")
    if normalized == "gaia":
        return _gaia_data_status()
    if normalized == "bfcl":
        configured = os.environ.get("CHATCOPILOT_BFCL_DATA_DIR", "").strip()
        if configured:
            return {"source": "configured", "cache_path": configured, "uses_smoke": False}
        cache = bfcl_cache_dir()
        if has_bfcl_official_data(cache):
            return {"source": "official_cache", "cache_path": str(cache), "uses_smoke": False}
        return {"source": "builtin_smoke", "cache_path": str(cache), "uses_smoke": True}
    if normalized == "ifeval":
        configured = os.environ.get("CHATCOPILOT_IFEVAL_DATA_PATH", "").strip()
        if configured:
            return {"source": "configured", "cache_path": configured, "uses_smoke": False}
        cache = ifeval_cache_path()
        if has_ifeval_official_data(cache):
            return {"source": "official_cache", "cache_path": str(cache), "uses_smoke": False}
        return {"source": "builtin_smoke", "cache_path": str(cache), "uses_smoke": True}
    return {"source": "", "cache_path": "", "uses_smoke": False}


def _gaia_data_status() -> dict[str, Any]:
    configured = os.environ.get("CHATCOPILOT_GAIA_DATA_PATH", "").strip()
    if configured:
        return {"source": "configured", "cache_path": configured, "uses_smoke": False}
    from chatcopilot.evals.adapters import gaia

    found = gaia.find_cached_data()
    if found:
        return {"source": "official_cache", "cache_path": found, "uses_smoke": False}
    if os.environ.get("CHATCOPILOT_GAIA_SMOKE", "").strip():
        return {"source": "builtin_smoke", "cache_path": "", "uses_smoke": True}
    return {"source": "unavailable", "cache_path": "", "uses_smoke": False}


def _download_file(url: str, target: Path) -> None:
    if target.is_file() and target.stat().st_size > 0:
        return
    import requests

    target.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=120, stream=True)
    response.raise_for_status()
    temp = target.with_suffix(target.suffix + ".tmp")
    with temp.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                handle.write(chunk)
    temp.replace(target)


__all__ = [
    "bfcl_cache_dir",
    "default_cache_root",
    "has_bfcl_official_data",
    "has_ifeval_official_data",
    "ifeval_cache_path",
    "prepare_official_data",
    "suite_data_status",
]
