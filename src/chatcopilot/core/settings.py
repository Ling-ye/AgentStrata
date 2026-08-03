"""Runtime environment helpers."""

from __future__ import annotations

import os
from pathlib import Path

from chatcopilot.project import ENV_PREFIX


BOT_SPEC_ENV = f"{ENV_PREFIX}_BOT_SPEC"


def get_bot_spec_env() -> Path | None:
    """Return the BotSpec path exported for the current process, if any."""

    raw = os.environ.get(BOT_SPEC_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def set_bot_spec_env(path: Path) -> None:
    """Expose the selected BotSpec path to child/runtime modules."""

    os.environ[BOT_SPEC_ENV] = str(path.expanduser().resolve())

