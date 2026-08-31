from __future__ import annotations

import io
import json
import stat
import textwrap
from pathlib import Path
import sys

import pytest

from chatcopilot.botspec.qq_token_sync import main, upsert_local_env_token
from chatcopilot.core.settings import load_local_env_values
from chatcopilot.platforms.qq.token_sync import read_and_validate_token


def _write_starter_bot(tmp_path: Path, extra_lines: str = "") -> tuple[Path, Path]:
    bot_dir = tmp_path / "bots" / "token-test-qq"
    bot_dir.parent.mkdir(parents=True)
    bot_dir.mkdir()
    bot_path = bot_dir / "bot.yaml"
    bot_path.write_text(
        textwrap.dedent(
            """\
            id: token-test-qq
            display_name: Token Test
            platform:
              type: qq
              adapter: qq_acp
            llm:
              chat:
                env_prefix: CHATCOPILOT_CHAT
            prompts:
              schema_version: 2
              identity: prompts/identity.md
              response_style: prompts/response-style.md
              refusal_style: prompts/refusal-style.md
            tools:
              packs:
              - workspace.read_write
              - memory.chat
              features:
              - chat.file_uploads
              - chat.private_workspace
            context:
              memory_store:
                provider: markdown
                namespace: token-test-qq
            agents:
              backend: native
              presets: []
            access:
              owner_only_project_access: true
            """
        ),
        encoding="utf-8",
    )
    env_path = bot_dir / "local.env"
    env_path.write_text(
        textwrap.dedent(
            f"""\
            export CHATCOPILOT_CHAT_API_KEY="test-key"
            export CHATCOPILOT_CHAT_BASE_URL="https://example.invalid/v1"
            export CHATCOPILOT_CHAT_MODEL="test-model"
            export CHATCOPILOT_ADD_OWNER_IDS="20002"
            export QQ_ACCOUNT="10001"
            export QQ_ACCESS_TOKEN="{'c' * 64}"
            export QQ_ALLOW_FROM="20002"
            {extra_lines}
            """
        ),
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    return bot_path, env_path


def test_upsert_local_env_token_preserves_advanced_and_unknown_keys(
    tmp_path: Path,
) -> None:
    bot_path, env_path = _write_starter_bot(
        tmp_path,
        'export NAPCAT_QUICK_PASSWORD="private-login"\n'
        'export CHATCOPILOT_LINGYE_CODE_MODEL="gpt-code"\n'
        'export UNKNOWN_FUTURE_KEY="preserve-me"',
    )
    token = "a" * 64

    receipt = upsert_local_env_token(
        env_path,
        token,
        bot_path=bot_path,
        bots_root=bot_path.parent.parent,
    )

    text = env_path.read_text(encoding="utf-8")
    assert load_local_env_values(env_path)["QQ_ACCESS_TOKEN"] == token
    assert text.count("QQ_ACCESS_TOKEN=") == 1
    assert 'NAPCAT_QUICK_PASSWORD="private-login"' in text
    assert 'CHATCOPILOT_LINGYE_CODE_MODEL="gpt-code"' in text
    assert 'UNKNOWN_FUTURE_KEY="preserve-me"' in text
    assert receipt.committed is True
    assert token not in str(receipt.to_dict())
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_upsert_local_env_token_inserts_after_qq_ws_url(tmp_path: Path) -> None:
    bot_path, env_path = _write_starter_bot(tmp_path, 'export KEEP_ME="yes"')

    upsert_local_env_token(
        env_path,
        "b" * 64,
        bot_path=bot_path,
        bots_root=bot_path.parent.parent,
    )

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert any(line == f"export QQ_ACCESS_TOKEN={'b' * 64}" for line in lines)
    assert any('export KEEP_ME="yes"' == line.strip() for line in lines)


def test_upsert_local_env_token_rejects_symlink(tmp_path: Path) -> None:
    bot_path, env_path = _write_starter_bot(tmp_path)
    victim = tmp_path / "victim.env"
    victim.write_bytes(env_path.read_bytes())
    victim.chmod(0o600)
    env_path.unlink()
    env_path.symlink_to(victim)

    with pytest.raises(ValueError, match="provision_target_unsafe"):
        upsert_local_env_token(
            env_path,
            "d" * 64,
            bot_path=bot_path,
            bots_root=bot_path.parent.parent,
        )

    assert env_path.is_symlink()
    assert b"d" * 64 not in victim.read_bytes()


def test_upsert_local_env_token_rejects_symlinked_bot_directory(
    tmp_path: Path,
) -> None:
    bots_root = tmp_path / "bots"
    bots_root.mkdir()
    outside = tmp_path / "outside" / "token-test-qq"
    outside.mkdir(parents=True)
    bot_path, env_path = _write_starter_bot(tmp_path / "source")
    for source in (bot_path, env_path):
        target = outside / source.name
        target.write_bytes(source.read_bytes())
        if target.name == "local.env":
            target.chmod(0o600)
    linked = bots_root / "token-test-qq"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="token_sync_path_unsafe"):
        upsert_local_env_token(
            linked / "local.env",
            "e" * 64,
            bot_path=linked / "bot.yaml",
            bots_root=bots_root,
        )

    assert load_local_env_values(outside / "local.env")["QQ_ACCESS_TOKEN"] == "c" * 64


def test_read_and_validate_token_rejects_invalid_stdin() -> None:
    with pytest.raises(ValueError, match="qq_access_token_invalid"):
        read_and_validate_token("weak-token")


def test_sync_cli_reads_stdin_and_returns_secret_free_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot_path, env_path = _write_starter_bot(tmp_path)
    token = "f" * 64
    monkeypatch.setattr(sys, "stdin", io.StringIO(token))

    result = main(
        [
            "--path",
            str(env_path),
            "--bot",
            str(bot_path),
            "--bots-root",
            str(bot_path.parent.parent),
        ]
    )

    output = capsys.readouterr()
    assert result == 0
    assert token not in output.out
    assert token not in output.err
    receipt = json.loads(output.out.splitlines()[-1])
    assert receipt["committed"] is True
    assert receipt["changed_fields"] == ["qq_access_token"]
    assert len(receipt["config_sha256"]) == 64


def test_sync_cli_invalid_token_preserves_original_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bot_path, env_path = _write_starter_bot(tmp_path)
    original = env_path.read_bytes()
    invalid_token = "weak-token"
    monkeypatch.setattr(sys, "stdin", io.StringIO(invalid_token))

    result = main(
        [
            "--path",
            str(env_path),
            "--bot",
            str(bot_path),
            "--bots-root",
            str(bot_path.parent.parent),
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert env_path.read_bytes() == original
    assert invalid_token not in output.out
    assert invalid_token not in output.err
