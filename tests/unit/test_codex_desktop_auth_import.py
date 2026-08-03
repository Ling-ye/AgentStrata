from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "wsl" / "import_codex_desktop_auth.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )


def test_retired_importer_refuses_without_reading_or_mutating_credentials(
    tmp_path: Path,
) -> None:
    source = tmp_path / "desktop-auth.json"
    source.write_text('{"token":"desktop-secret"}', encoding="utf-8")
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    target = authority / "auth.json"
    target.write_text('{"token":"existing"}', encoding="utf-8")
    target.chmod(0o600)
    before = target.read_bytes()

    result = _run(
        "--instance",
        "lingye-copilot-qq",
        "--source",
        str(source),
        "--target",
        str(authority),
    )

    assert result.returncode == 1
    assert "code=desktop_auth_import_retired" in result.stderr
    assert "python -m chatcopilot bot codex-auth login" in result.stderr
    assert "desktop-secret" not in result.stdout
    assert "desktop-secret" not in result.stderr
    assert target.read_bytes() == before


def test_retired_importer_refuses_unknown_and_empty_arguments() -> None:
    empty = _run()
    unknown = _run("--anything", "secret")

    assert empty.returncode == 1
    assert unknown.returncode == 1
    assert "code=desktop_auth_import_retired" in empty.stderr
    assert "secret" not in unknown.stdout
    assert "secret" not in unknown.stderr


def test_retired_importer_help_only_describes_replacement() -> None:
    result = _run("--help")

    assert result.returncode == 0
    assert "retired" in result.stdout
    assert "codex-auth login" in result.stdout
