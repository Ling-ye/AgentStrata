"""Environment normalization and deterministic parsing shared by evals."""
from __future__ import annotations

import os
import re


EVAL_PATH_KEYS = frozenset(
    {
        "CHATCOPILOT_GAIA_DATA_PATH",
        "CHATCOPILOT_GAIA_FILES_DIR",
        "CHATCOPILOT_GAIA_MANIFEST_PATH",
        "CHATCOPILOT_BFCL_DATA_DIR",
        "CHATCOPILOT_IFEVAL_DATA_PATH",
        "CHATCOPILOT_EVALS_DATA_DIR",
    }
)


def normalize_eval_env_value(key: str, value: str) -> str:
    """Convert Windows drive paths to WSL mount paths on POSIX hosts."""
    if os.name == "nt" or key not in EVAL_PATH_KEYS:
        return value
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
    if not match:
        return value
    drive = match.group(1).lower()
    tail = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{tail}"


def normalize_eval_env(values: dict[str, str]) -> dict[str, str]:
    return {
        key: normalize_eval_env_value(key, value)
        for key, value in values.items()
    }


def positive_int_from_env(name: str) -> int | None:
    """Return a positive integer or ``None`` for unset/invalid input."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


__all__ = [
    "EVAL_PATH_KEYS",
    "normalize_eval_env",
    "normalize_eval_env_value",
    "positive_int_from_env",
]
