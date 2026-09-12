"""Local NLTK resource location; reading never downloads data."""
from pathlib import Path
import os

REVISION = "550b6625bcef1f2abff2ff770a5a0d272c9c6b2a"


def resource_dir() -> Path:
    root = Path(os.environ.get("CHATCOPILOT_EVALS_DATA_DIR") or "~/.cache/agentstrata/evals").expanduser()
    return root / "ifeval" / "resources" / REVISION


def configure_resources() -> None:
    import nltk

    path = str(resource_dir())
    if path not in nltk.data.path:
        nltk.data.path.insert(0, path)
