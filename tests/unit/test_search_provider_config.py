from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.model import SearchProviderSpec as BotSpecSearchProviderSpec
from chatcopilot.contracts import SearchProviderSpec


_TAVILY_USERINFO_ENDPOINT = "https" + "://token" + "@" + "api.tavily.com/search"


def test_search_provider_contract_reexports_are_canonical() -> None:
    assert BotSpecSearchProviderSpec is SearchProviderSpec


def _write_bot(tmp_path: Path, unified_search: str) -> Path:
    bot_dir = tmp_path / "test-bot"
    bot_dir.mkdir()
    (bot_dir / "persona.md").write_text("test", encoding="utf-8")
    body = textwrap.indent(textwrap.dedent(unified_search).strip(), "    ")
    path = bot_dir / "bot.yaml"
    path.write_text(
        "id: test-bot\n"
        "display_name: Test Bot\n"
        "platform:\n"
        "  type: qq\n"
        "  adapter: qq_acp\n"
        "prompts:\n"
        "  schema_version: 2\n"
        "  identity: persona.md\n"
        "  response_style: persona.md\n"
        "tools:\n"
        "  packs: []\n"
        "agents:\n"
        "  backend: native\n"
        "  unified_search:\n"
        f"{body}\n"
        "deploy:\n"
        "  target: wsl2\n",
        encoding="utf-8",
    )
    return path


def _provider_errors(path: Path) -> list[str]:
    return [
        issue.message
        for issue in validate_botspec(load_botspec(path))
        if issue.level == "error"
        and issue.field.startswith("agents.unified_search.providers")
    ]


def test_unified_search_provider_config_parses_in_declared_order(tmp_path: Path) -> None:
    path = _write_bot(
        tmp_path,
        """
        enabled: true
        providers:
          - id: tavily
            kind: tavily
            endpoint: https://api.tavily.com/search
            credential_env: TAVILY_API_KEY
            timeout_seconds: 15
            max_results: 10
          - id: local-search
            kind: searxng
            endpoint: http://127.0.0.1:18064
            timeout_seconds: 20
            max_results: 8
        """,
    )

    spec = load_botspec(path)

    assert _provider_errors(path) == []
    assert spec.agents.research_enabled is True
    assert [provider.id for provider in spec.agents.search_providers] == [
        "tavily",
        "local-search",
    ]
    assert spec.agents.search_providers[0].credential_env == "TAVILY_API_KEY"
    assert spec.agents.search_providers[1].credential_env is None
    assert spec.agents.search_providers[1].timeout_seconds == 20.0


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        (
            """
            - {id: duplicate, kind: tavily}
            - {id: duplicate, kind: searxng}
            """,
            "duplicate unified-search provider id",
        ),
        ("- {id: custom, kind: unknown}", "provider kind must be one of"),
        ("- {id: BAD_ID, kind: tavily}", "provider id must be kebab-case"),
        (
            "- {id: tavily, kind: tavily, credential_env: lowercase_key}",
            "credential_env must be an uppercase",
        ),
        (
            "- {id: tavily, kind: tavily, timeout_seconds: 61}",
            "timeout_seconds must be between",
        ),
        (
            "- {id: tavily, kind: tavily, max_results: 16}",
            "max_results must be between",
        ),
        (
            "- {id: tavily, kind: tavily, endpoint: https://api.tavily.com.evil.example/search}",
            "endpoint must be exactly",
        ),
        (
            "- {id: brave, kind: brave, endpoint: "
            "https://api.search.brave.com:444/res/v1/web/search}",
            "endpoint must be exactly",
        ),
        (
            f"- {{id: tavily, kind: tavily, endpoint: {_TAVILY_USERINFO_ENDPOINT}}}",
            "must not contain credentials",
        ),
        (
            "- {id: searxng, kind: searxng, endpoint: http://searxng:8080}",
            "literal loopback host",
        ),
    ],
)
def test_unified_search_provider_validation_is_fail_closed(
    tmp_path: Path,
    providers: str,
    expected: str,
) -> None:
    indented = textwrap.indent(textwrap.dedent(providers).strip(), "  ")
    path = _write_bot(tmp_path, f"enabled: true\nproviders:\n{indented}")

    assert any(expected in message for message in _provider_errors(path))


def test_unified_search_provider_rejects_unknown_fields(tmp_path: Path) -> None:
    path = _write_bot(
        tmp_path,
        """
        enabled: true
        providers:
          - id: tavily
            kind: tavily
            api_key: tracked-secret
        """,
    )

    with pytest.raises(ValueError, match="unsupported field.*api_key"):
        load_botspec(path)
