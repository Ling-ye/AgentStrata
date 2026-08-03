"""Runtime bootstrap helpers for the WSL console process."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_src_path() -> None:
    """Make the src-layout package importable when running console in-place."""
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    if not src.is_dir():
        return
    src_text = str(src)
    if src_text not in sys.path:
        sys.path.insert(0, src_text)
    current = os.environ.get("PYTHONPATH", "")
    paths = [item for item in current.split(os.pathsep) if item]
    if src_text not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([src_text, *paths])


__all__ = ["ensure_src_path"]
