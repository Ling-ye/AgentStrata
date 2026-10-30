"""Published shell entrypoints remain usable after moving their implementations."""
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("entry,implementation,arguments", [
    ("console/scripts/ctl.sh", "deploy/wsl/instance_control.sh", ["restart", "fixture-bot"]),
    ("console/systemd/register.sh", "deploy/wsl/register_instance.sh", ["--enable", "fixture-bot"]),
])
def test_public_shell_entry_forwards_arguments_and_exit_status(tmp_path, entry, implementation, arguments):
    public = tmp_path / entry
    public.parent.mkdir(parents=True)
    public.write_bytes((ROOT / entry).read_bytes())
    target = tmp_path / implementation
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\nexit 17\n')
    result = subprocess.run(["bash", str(public), *arguments], capture_output=True, text=True, check=False)
    assert result.returncode == 17
    assert result.stdout.splitlines() == arguments
