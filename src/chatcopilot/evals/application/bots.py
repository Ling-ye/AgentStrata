"""Bot references and evaluation-owned environment loading."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.runtime_env import llm_runtime_env_defaults
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.evals.env import normalize_eval_env


_ENV_LOCK = threading.RLock()
_EVAL_ENV_SNAPSHOT_MARKER = "CHATCOPILOT_EVALUATION_ENV_SNAPSHOT"


class _EvaluationEnvironmentSnapshot(dict[str, str]):
    """Process-local capability proving an environment was already captured."""

    def copy(self) -> _EvaluationEnvironmentSnapshot:
        return type(self)(self)


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
    spec_path = bot_spec_path(bot, repository_root)
    spec = load_botspec(spec_path)
    local_values = llm_runtime_env_defaults(spec.llm)
    local_values.update(
        load_local_env_values(
            spec_path.parent / "local.env",
            missing_ok=True,
            expand_home=True,
        )
    )
    local_values = normalize_eval_env(local_values)
    return _effective_environment_snapshot(local_values)


def _effective_environment_snapshot(
    values: Mapping[str, str],
) -> _EvaluationEnvironmentSnapshot:
    """Capture machine-first runtime precedence exactly once.

    A value set by the service/machine environment is authoritative. Bot-local
    values only fill missing keys, matching normal runtime loading. Only the
    process-local snapshot type is trusted as already captured; the public
    marker is runtime coordination metadata and cannot grant that trust.
    """

    with _ENV_LOCK:
        if isinstance(values, _EvaluationEnvironmentSnapshot):
            return values.copy()
        environment = dict(values)
        # A bot-local local.env must not be able to forge an immutable snapshot
        # and bypass machine-first precedence.
        environment.pop(_EVAL_ENV_SNAPSHOT_MARKER, None)
        environment.update(os.environ)
        environment[_EVAL_ENV_SNAPSHOT_MARKER] = "1"
        return _EvaluationEnvironmentSnapshot(environment)


@contextmanager
def temporary_eval_env(values: Mapping[str, str]) -> Iterator[None]:
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


def evaluation_subprocess_env(values: Mapping[str, str]) -> dict[str, str]:
    """Build the same immutable effective environment used by preflight."""

    return _effective_environment_snapshot(values)


__all__ = [
    "EvaluationBotRef",
    "EvaluationBotResolver",
    "bot_env",
    "bot_spec_path",
    "evaluation_subprocess_env",
    "temporary_eval_env",
]
