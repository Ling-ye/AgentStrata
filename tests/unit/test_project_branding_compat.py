from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from chatcopilot.project import COMPAT_DATA_DIRNAME, PROJECT_NAME


def test_public_name_does_not_move_compatibility_data_directories(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop("CHATCOPILOT_HOME", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from chatcopilot.project import DEFAULT_HOME; print(DEFAULT_HOME)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert PROJECT_NAME == "AgentStrata"
    assert COMPAT_DATA_DIRNAME == "ChatCopilot"
    assert completed.stdout.strip() == str(tmp_path / "ChatCopilot")
