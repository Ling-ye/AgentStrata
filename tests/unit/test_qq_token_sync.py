from __future__ import annotations

import stat
from pathlib import Path

import pytest

from chatcopilot.platforms.qq.token_sync import (
    read_and_validate_token,
    upsert_local_env_token,
)


def test_upsert_local_env_token_preserves_advanced_and_unknown_keys(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "local.env"
    env_path.write_text(
        "\n".join(
            (
                'export CHATCOPILOT_LINGYE_MODEL="gpt-test"',
                'export NAPCAT_QUICK_PASSWORD="private-login"',
                'export QQ_WS_URL="ws://127.0.0.1:3001"',
                'export QQ_ACCESS_TOKEN=""',
                'export CHATCOPILOT_LINGYE_CODE_MODEL="gpt-code"',
                'export UNKNOWN_FUTURE_KEY="preserve-me"',
                "",
            )
        ),
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    token = "a" * 64

    upsert_local_env_token(env_path, token)

    text = env_path.read_text(encoding="utf-8")
    assert 'export QQ_ACCESS_TOKEN="a' in text
    assert text.count("QQ_ACCESS_TOKEN=") == 1
    assert 'NAPCAT_QUICK_PASSWORD="private-login"' in text
    assert 'CHATCOPILOT_LINGYE_CODE_MODEL="gpt-code"' in text
    assert 'UNKNOWN_FUTURE_KEY="preserve-me"' in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_upsert_local_env_token_inserts_after_qq_ws_url(tmp_path: Path) -> None:
    env_path = tmp_path / "local.env"
    env_path.write_text(
        'export QQ_WS_URL="ws://127.0.0.1:3001"\nexport KEEP_ME="yes"\n',
        encoding="utf-8",
    )

    upsert_local_env_token(env_path, "b" * 64)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == f'export QQ_ACCESS_TOKEN="{"b" * 64}"'
    assert lines[2] == 'export KEEP_ME="yes"'


def test_read_and_validate_token_rejects_invalid_stdin() -> None:
    with pytest.raises(ValueError, match="qq_access_token_invalid"):
        read_and_validate_token("weak-token")
