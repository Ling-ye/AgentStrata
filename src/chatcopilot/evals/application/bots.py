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


_ENV_LOCK = threading.RLock()
_EVAL_ENV_SNAPSHOT_MARKER = "CHATCOPILOT_EVALUATION_ENV_SNAPSHOT"


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
    local_values = normalize_eval_env(
        _load_env_values(bot_spec_path(bot, repository_root).parent / "local.env")
    )
    return _effective_environment_snapshot(local_values)


def _effective_environment_snapshot(values: dict[str, str]) -> dict[str, str]:
    """Capture machine-first runtime precedence exactly once.

    A value set by the service/machine environment is authoritative. Bot-local
    values only fill missing keys, matching normal runtime loading. A mapping
    that already carries the private marker is an immutable captured snapshot
    and must not be merged with later process-environment changes.
    """

    with _ENV_LOCK:
        if values.get(_EVAL_ENV_SNAPSHOT_MARKER) == "1":
            return dict(values)
        environment = dict(values)
        environment.update(os.environ)
        environment[_EVAL_ENV_SNAPSHOT_MARKER] = "1"
        return environment


@contextmanager
def temporary_eval_env(values: dict[str, str]) -> Iterator[None]:
    """Apply one immutable effective environment for preflight/fingerprint."""

    with _ENV_LOCK:
        effective = _effective_environment_snapshot(values)
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(effective)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(old)


def evaluation_subprocess_env(values: dict[str, str]) -> dict[str, str]:
    """Build the same immutable effective environment used by preflight."""

    return _effective_environment_snapshot(values)


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
