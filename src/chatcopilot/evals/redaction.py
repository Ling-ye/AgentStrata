"""Fail-closed sanitization for persisted evaluation evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|authorization|credential|password|secret|"
    r"session[_-]?token|(?:^|[_-])token(?:$|[_-]))",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|authorization|credential|password|secret|"
    r"session[_-]?token|(?:[A-Za-z0-9_-]+[_-])?token)\s*[:=]\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
_FILE_URI = re.compile(r"(?i)file:///(?:[^\s'\"`,;|<>()\[\]{}]+)")
_WINDOWS_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|\\\\)[^\s'\"`,;|<>()\[\]{}]+"
)
_POSIX_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9_$\\/.])/(?!/)[^\s'\"`,;|<>()\[\]{}]+"
)


def collect_env_secrets(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    values = os.environ if env is None else env
    secrets = {
        str(value)
        for key, value in values.items()
        if _SECRET_KEY.search(str(key)) and len(str(value)) >= 6
    }
    return tuple(sorted(secrets, key=len, reverse=True))


def redact_payload(
    value: Any,
    *,
    secrets: Iterable[str] = (),
    roots: Mapping[str, str | Path] | None = None,
) -> Any:
    normalized_roots = {
        label: str(path)
        for label, path in (roots or {}).items()
        if str(path).strip()
    }
    secret_values = tuple(item for item in secrets if len(str(item)) >= 6)
    return _redact(value, secrets=secret_values, roots=normalized_roots, key="")


def sanitize_text(
    text: str,
    *,
    secrets: Iterable[str] = (),
    roots: Mapping[str, str | Path] | None = None,
) -> str:
    result = str(text)
    for secret in secrets:
        if len(str(secret)) >= 6:
            result = result.replace(str(secret), "[REDACTED]")
    for label, raw_path in (roots or {}).items():
        path = str(raw_path).rstrip("/\\")
        if path:
            result = result.replace(path, f"${label.upper()}")
            result = result.replace(path.replace("\\", "/"), f"${label.upper()}")
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _INLINE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", result)
    result = _FILE_URI.sub("$ABSOLUTE_PATH", result)
    result = _WINDOWS_ABSOLUTE.sub("$ABSOLUTE_PATH", result)
    result = _POSIX_ABSOLUTE.sub("$ABSOLUTE_PATH", result)
    return result


def _redact(value: Any, *, secrets: tuple[str, ...], roots: Mapping[str, str], key: str) -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(
                item_value,
                secrets=secrets,
                roots=roots,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets=secrets, roots=roots, key=key) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, secrets=secrets, roots=roots)
    return value


__all__ = ["collect_env_secrets", "redact_payload", "sanitize_text"]
