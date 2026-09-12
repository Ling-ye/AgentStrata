"""Official benchmark data preparation for eval adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ENV_CACHE_ROOT = "CHATCOPILOT_EVALS_DATA_DIR"


def default_cache_root() -> Path:
    raw = os.environ.get(_ENV_CACHE_ROOT, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "agentstrata" / "evals"


def bfcl_cache_dir() -> Path:
    from chatcopilot.evals.llm_data import bfcl_dir
    return bfcl_dir()


def ifeval_cache_path() -> Path:
    from chatcopilot.evals.llm_data import ifeval_path
    return ifeval_path()


def has_bfcl_official_data(path: Path | None = None) -> bool:
    from chatcopilot.evals.llm_data import verified, BFCL_REVISION, BFCL_FILES
    return verified(path or bfcl_cache_dir(), BFCL_REVISION, BFCL_FILES)


def has_ifeval_official_data(path: Path | None = None) -> bool:
    from chatcopilot.evals.llm_data import verified, IFEVAL_REVISION
    return verified((path or ifeval_cache_path()).parent, IFEVAL_REVISION, ("input_data.jsonl",))


def prepare_bfcl_official_data() -> dict[str, Any]:
    from chatcopilot.evals.llm_data import prepare_bfcl
    return {"suite_id": "bfcl", **prepare_bfcl()}


def prepare_ifeval_official_data() -> dict[str, Any]:
    from chatcopilot.evals.llm_data import prepare_ifeval
    return {"suite_id": "ifeval", **prepare_ifeval()}


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
    if normalized == "swe-bench-verified":
        from chatcopilot.evals.benchmark_data import prepare_swe_data

        return {"suite_id": normalized, **prepare_swe_data()}
    raise ValueError(f"{normalized} does not support official data preparation")


def suite_data_status(suite_id: str) -> dict[str, Any]:
    normalized = suite_id.strip().lower().replace("_", "-")
    if normalized in {"swe-bench-verified", "agentbench-fc"}:
        key = "CHATCOPILOT_SWEBENCH_DATA_PATH" if normalized == "swe-bench-verified" else "CHATCOPILOT_AGENTBENCH_DATA_PATH"
        configured = os.environ.get(key, "").strip()
        if not configured and normalized == "swe-bench-verified":
            from chatcopilot.evals.benchmark_data import swe_data_path

            cached = swe_data_path()
            if cached:
                return {"source": "official_cache", "cache_path": str(cached), "uses_smoke": False}
        return {"source": "configured" if configured else "unavailable", "cache_path": configured, "uses_smoke": False}
    if normalized == "gaia":
        return _gaia_data_status()
    if normalized == "bfcl":
        if os.environ.get("CHATCOPILOT_BFCL_CASE_PROFILE", "").strip() == "smoke":
            return {"source": "builtin_smoke", "cache_path": "", "uses_smoke": True}
        configured = os.environ.get("CHATCOPILOT_BFCL_DATA_DIR", "").strip()
        if configured:
            return {"source": "configured", "cache_path": configured, "uses_smoke": False}
        cache = bfcl_cache_dir()
        if has_bfcl_official_data(cache):
            return {"source": "official_cache", "cache_path": str(cache), "uses_smoke": False}
        return {"source": "builtin_smoke" if os.environ.get("CHATCOPILOT_BFCL_CASE_PROFILE") == "smoke" else "unavailable", "cache_path": str(cache), "uses_smoke": os.environ.get("CHATCOPILOT_BFCL_CASE_PROFILE") == "smoke"}
    if normalized == "ifeval":
        if os.environ.get("CHATCOPILOT_IFEVAL_CASE_PROFILE", "").strip() == "smoke":
            return {"source": "fixed_official_subset", "cache_path": "", "uses_smoke": True}
        configured = os.environ.get("CHATCOPILOT_IFEVAL_DATA_PATH", "").strip()
        if configured:
            return {"source": "configured", "cache_path": configured, "uses_smoke": False}
        cache = ifeval_cache_path()
        if has_ifeval_official_data(cache):
            return {"source": "official_cache", "cache_path": str(cache), "uses_smoke": False}
        return {"source": "fixed_official_subset" if os.environ.get("CHATCOPILOT_IFEVAL_CASE_PROFILE") == "smoke" else "unavailable", "cache_path": str(cache), "uses_smoke": False}
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



__all__ = [
    "bfcl_cache_dir",
    "default_cache_root",
    "has_bfcl_official_data",
    "has_ifeval_official_data",
    "ifeval_cache_path",
    "prepare_official_data",
    "suite_data_status",
]
