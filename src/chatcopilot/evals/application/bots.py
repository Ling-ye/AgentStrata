"""Bot references and evaluation-owned environment loading."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.evals.env import normalize_eval_env


@dataclass(frozen=True)
class EvaluationBotRef:
    """Minimal immutable Bot identity required by the Evaluation service."""

    instance_id: str
    bot_spec: Path


class EvaluationBotResolver:
    """Resolve deployed instance ids to repository-contained BotSpecs."""

    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root.expanduser().resolve()

    def __call__(self, instance_id: str) -> EvaluationBotRef:
        requested = str(instance_id or "").strip()
        if not requested:
            raise KeyError(requested)
        bots_root = (self.repository_root / "bots").resolve()
        try:
            bots_root.relative_to(self.repository_root)
        except ValueError as exc:
            raise RuntimeError("BotSpec root escapes the repository") from exc
        matches: list[EvaluationBotRef] = []
        for candidate in sorted(bots_root.glob("*/bot.yaml")):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(bots_root)
            except ValueError:
                continue
            if not resolved.is_file():
                continue
            spec = load_botspec(resolved)
            resolved_id = spec.deploy.instance_id or spec.id
            if requested == resolved_id:
                matches.append(
                    EvaluationBotRef(
                        instance_id=resolved_id,
                        bot_spec=resolved,
                    )
                )
        if len(matches) != 1:
            raise KeyError(requested)
        return matches[0]


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


def bot_spec_path(bot: EvaluationBotRef, repository_root: Path) -> Path:
    repository = repository_root.expanduser().resolve()
    raw = Path(bot.bot_spec)
    path = raw if raw.is_absolute() else repository / raw
    resolved = path.resolve()
    try:
        resolved.relative_to(repository)
    except ValueError as exc:
        raise ValueError(f"BotSpec escapes repository: {bot.instance_id}") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"BotSpec not found: {bot.instance_id}")
    return resolved


def bot_env(bot: EvaluationBotRef, repository_root: Path) -> dict[str, str]:
    return normalize_eval_env(
        _load_env_values(bot_spec_path(bot, repository_root).parent / "local.env")
    )


@contextmanager
def temporary_eval_env(values: dict[str, str]) -> Iterator[None]:
    """Apply bot-owned evaluation settings for one serialized operation."""

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


def evaluation_subprocess_env(values: dict[str, str]) -> dict[str, str]:
    """Build a private child environment without exposing temporary bot values."""

    with _ENV_LOCK:
        environment = os.environ.copy()
        for key, value in values.items():
            if key in _EVAL_ENV_KEYS:
                environment[key] = value
        return environment


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


__all__ = [
    "EvaluationBotRef",
    "EvaluationBotResolver",
    "bot_env",
    "bot_spec_path",
    "evaluation_subprocess_env",
    "temporary_eval_env",
]
