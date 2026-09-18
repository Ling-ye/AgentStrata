"""Operator configuration for the standalone worker; never a Bot environment loader."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Mapping
from chatcopilot.core.observability_redaction import redact_observability_payload

from chatcopilot.core.private_sqlite import private_file

ENVIRONMENT_KEYS = frozenset(
    {
        "CHATCOPILOT_HARNESS_ROOT",
        "CHATCOPILOT_HARNESS_GITHUB_REPOSITORY",
        "CHATCOPILOT_HARNESS_GITHUB_ACTOR",
        "CHATCOPILOT_HARNESS_GITHUB_TOKEN_FILE",
        "CHATCOPILOT_HARNESS_GIT_AUTHOR_NAME",
        "CHATCOPILOT_HARNESS_GIT_AUTHOR_EMAIL",
        "CHATCOPILOT_HARNESS_MODEL",
        "CHATCOPILOT_HARNESS_UV_BIN",
        "CHATCOPILOT_CODEX_BIN",
        "CHATCOPILOT_CODEX_BOT_HOME",
        "CHATCOPILOT_EVALUATION_SOCKET",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
)


def configuration() -> dict[str, str]:
    path = Path(
        os.environ.get("CHATCOPILOT_HARNESS_ENV")
        or Path.home() / ".config" / "agentstrata" / "harness.env"
    ).expanduser()
    values: dict[str, str] = {}
    if path.exists() or path.is_symlink():
        private_file(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, separator, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not separator or key not in ENVIRONMENT_KEYS:
                    raise ValueError("Harness configuration contains an unsupported setting")
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                for prefix in ("${HOME}", "$HOME", "~"):
                    if value == prefix or value.startswith(prefix + "/"):
                        value = str(Path.home()) + value[len(prefix) :]
                        break
                if value:
                    values[key] = value
    values.update({key: os.environ[key] for key in ENVIRONMENT_KEYS if os.environ.get(key)})
    return values


def default_root(repository: Path, settings: Mapping[str, str] | None = None) -> Path:
    configured = (configuration() if settings is None else settings).get("CHATCOPILOT_HARNESS_ROOT")
    if configured:
        return Path(configured).expanduser().absolute()
    identity = hashlib.sha256(str(repository.resolve()).encode()).hexdigest()[:16]
    return Path.home() / ".local" / "state" / "agentstrata" / "harness" / identity


def safe_error(error: Exception, extra_secrets: tuple[str, ...] = ()) -> str:
    secrets = (
        *extra_secrets,
        *(
            value
            for name, value in os.environ.items()
            if any(
                part in name.lower() for part in ("secret", "token", "password", "api_key", "proxy")
            )
        ),
    )
    value = redact_observability_payload({"error": str(error)}, secrets=secrets).value
    return str(value.get("error", type(error).__name__))[:1000]
