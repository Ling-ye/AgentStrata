"""Operator configuration for the standalone worker; never a Bot environment loader."""

from __future__ import annotations

import os
from pathlib import Path

from chatcopilot.core.private_sqlite import private_file

ENVIRONMENT_KEYS = frozenset(
    {
        "CHATCOPILOT_HARNESS_ROOT",
        "CHATCOPILOT_HARNESS_MODEL",
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
