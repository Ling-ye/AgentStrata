"""BotSpec path resolution shared by BotSpec, tools, and control surfaces."""
from __future__ import annotations

from pathlib import Path


def resolve_bot_spec_path(value: str | Path, *, repo_root: Path | None = None) -> Path:
    raw = Path(value).expanduser()
    if raw.suffix in {".yaml", ".yml"} or raw.name == "bot.yaml":
        return raw.resolve()

    root = repo_root or Path.cwd()
    return (root / "bots" / str(value) / "bot.yaml").resolve()


__all__ = ["resolve_bot_spec_path"]
