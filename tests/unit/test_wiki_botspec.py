from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.wiki import resolve_wiki_root


def _write_bot(tmp_path: Path, wiki_yaml: str) -> Path:
    bot_dir = tmp_path / "wiki-bot"
    (bot_dir / "prompts").mkdir(parents=True)
    (bot_dir / "prompts" / "persona.md").write_text("Wiki bot\n", encoding="utf-8")
    path = bot_dir / "bot.yaml"
    path.write_text(
        "\n".join(
            [
                "id: wiki-bot",
                "display_name: Wiki Bot",
                "platform:",
                "  type: qq",
                "  adapter: qq_acp",
                "prompts:",
                "  schema_version: 2",
                "  identity: prompts/persona.md",
                "  response_style: prompts/persona.md",
                "tools:",
                "  packs: [wiki.knowledge]",
                "context:",
                "  wiki:",
                *[f"    {line}" for line in wiki_yaml.splitlines()],
                "deploy:",
                "  target: wsl2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_wiki_config_validates_without_machine_root(tmp_path: Path) -> None:
    spec = load_botspec(
        _write_bot(
            tmp_path,
            "\n".join(
                [
                    "enabled: true",
                    "root_env: PRIVATE_WIKI_ROOT",
                    "read_role: owner",
                    "private_chat_only: true",
                    "max_chunk_chars: 800",
                ]
            ),
        )
    )

    with mock.patch.dict(os.environ, {}, clear=True):
        errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

    assert errors == []
    assert spec.context.wiki.root_env == "PRIVATE_WIKI_ROOT"
    assert spec.context.wiki.max_chunk_chars == 800


def test_wiki_config_rejects_path_as_env_name_and_bad_role(tmp_path: Path) -> None:
    spec = load_botspec(
        _write_bot(
            tmp_path,
            "\n".join(
                [
                    "enabled: true",
                    "root_env: /tmp/wiki",
                    "read_role: guest",
                    "max_chunk_chars: 100",
                ]
            ),
        )
    )

    errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

    assert {issue.field for issue in errors} >= {
        "context.wiki.root_env",
        "context.wiki.read_role",
        "context.wiki.max_chunk_chars",
    }


@pytest.mark.parametrize("field", ("enabled", "private_chat_only"))
@pytest.mark.parametrize("value", ("invalid", ""), ids=("string", "null"))
def test_wiki_config_rejects_invalid_boolean(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=rf"context\.wiki\.{field}"):
        load_botspec(_write_bot(tmp_path, f"{field}: {value}"))


def test_resolve_wiki_root_uses_configured_env(tmp_path: Path) -> None:
    spec = load_botspec(_write_bot(tmp_path, "enabled: true\nroot_env: PRIVATE_WIKI_ROOT"))
    root = tmp_path / "private-wiki"

    with mock.patch.dict(os.environ, {"PRIVATE_WIKI_ROOT": str(root)}, clear=True):
        assert resolve_wiki_root(spec) == root.resolve()
