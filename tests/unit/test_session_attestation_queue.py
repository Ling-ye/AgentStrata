from __future__ import annotations

from pathlib import Path

import pytest

from chatcopilot.botspec import cli as botspec_cli


REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_SPEC = REPO_ROOT / "bots" / "lingye-copilot-qq" / "bot.yaml"


@pytest.mark.parametrize("session_key", ["qq:20002", "qq:g:30003"])
def test_gateway_qq_never_creates_legacy_session_attestation_state(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    session_key: str,
) -> None:
    directory = tmp_path / "session-env"

    exit_code = botspec_cli.main(
        [
            "render-session-env",
            "--bot",
            str(BOT_SPEC),
            "--session-key",
            session_key,
            "--session-env-dir",
            str(directory),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "qq_gateway_has_no_session_env" in captured.err
    assert not directory.exists()


def test_gateway_qq_rejection_preserves_preexisting_legacy_state(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session-env"
    directory.mkdir()
    sentinel = directory / "legacy-state"
    sentinel.write_bytes(b"must-not-change")

    exit_code = botspec_cli.main(
        [
            "render-session-env",
            "--bot",
            str(BOT_SPEC),
            "--session-key",
            "qq:g:30003",
            "--session-env-dir",
            str(directory),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "qq_gateway_has_no_session_env" in captured.err
    assert sentinel.read_bytes() == b"must-not-change"
    assert list(directory.iterdir()) == [sentinel]


def test_gateway_qq_never_reads_legacy_session_state_to_start_runtime(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    directory = tmp_path / "missing-session-env"

    exit_code = botspec_cli.main(
        [
            "exec-session-runtime",
            "--bot",
            str(BOT_SPEC),
            "--session-env-dir",
            str(directory),
            "--session-key",
            "qq:g:30003",
            "--",
            "--help",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "qq_gateway_has_no_session_runtime" in captured.err
    assert not directory.exists()
