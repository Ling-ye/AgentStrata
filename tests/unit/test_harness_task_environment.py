"""Frozen dependency profiles for complete Harness repository regression."""
from pathlib import Path
import json

import pytest

from chatcopilot.harness import task_environment
from chatcopilot.harness.models import HarnessError


def test_full_test_dependencies_are_installed_and_bound_to_receipt(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (source / "uv.lock").write_text("version = 1\n")
    uv = tmp_path / "uv"
    uv.touch()
    monkeypatch.setenv("CHATCOPILOT_HARNESS_UV_BIN", str(uv))
    calls = []

    class Process:
        returncode = 0

        def poll(self):
            return 0

    def popen(command, **kwargs):
        calls.append(command)
        python = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]) / "bin/python"
        python.parent.mkdir(parents=True)
        python.touch()
        return Process()

    monkeypatch.setattr(task_environment.subprocess, "Popen", popen)
    directory = tmp_path / "task"
    state = task_environment.prepare_environment(directory, source, lambda: None)
    command = calls[0]
    extras = [command[i + 1] for i, arg in enumerate(command) if arg == "--extra"]
    assert extras == ["agent", "acp", "dev", "evaluation"]
    assert "--frozen" in command and "--no-install-project" in command
    assert task_environment.prepare_environment(directory, source, lambda: None) == state
    assert len(calls) == 1

    receipt = directory / "environment/receipt.json"
    receipt.write_text(json.dumps({**state, "extras": "agent,acp,dev"}))
    with pytest.raises(HarnessError) as caught:
        task_environment.prepare_environment(directory, source, lambda: None)
    assert caught.value.code == "environment_changed"
    assert len(calls) == 1
