"""Persistent client model assets, separate from the pinned upstream source receipt."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from chatcopilot.core.install_artifacts import InstallError, private_directory, write_private


def prepare_assets(root: Path, backend: Path) -> None:
    destination = root / "data/live2d"
    if destination.is_symlink():
        raise InstallError("Live2D assets cannot be a symlink")
    if destination.exists():
        if not (destination / "model_dict.json").is_file() or not (destination / "models").is_dir():
            raise InstallError("Persistent Live2D assets are incomplete")
        return
    private_directory(root / "data")
    with tempfile.TemporaryDirectory(prefix=".live2d-", dir=root / "data") as temporary:
        staged = Path(temporary) / "live2d"
        private_directory(staged)
        dictionary = json.loads((backend / "model_dict.json").read_text())
        if not isinstance(dictionary, list):
            raise InstallError("Upstream Live2D model dictionary is invalid")
        shutil.copytree(backend / "live2d-models", staged / "models")
        write_private(staged / "model_dict.json", json.dumps(dictionary, ensure_ascii=False, indent=2))
        staged.rename(destination)


def runtime_assets(root: Path) -> dict[str, str]:
    if (root / "config/pending-model-import.json").exists():
        raise InstallError("Interrupted model import: restore its backup before starting the client")
    assets = root / "data/live2d"
    if assets.resolve() != assets or not (assets / "model_dict.json").is_file():
        raise InstallError("Persistent Live2D assets are unavailable")
    return {"AGENTSTRATA_VTUBER_MODELS": str(assets / "models"),
            "AGENTSTRATA_VTUBER_MODEL_DICT": str(assets / "model_dict.json")}
