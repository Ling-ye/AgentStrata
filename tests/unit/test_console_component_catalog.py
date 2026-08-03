from __future__ import annotations

import textwrap
from pathlib import Path

from console.control.instances import BotInstance


ROOT = Path(__file__).resolve().parents[2]


def test_component_catalog_exposes_mcp_entries() -> None:
    from chatcopilot.component_catalog import get_mcp_catalog_entry, iter_mcp_catalog_entries

    entries = dict(iter_mcp_catalog_entries())
    tavily = get_mcp_catalog_entry("tavily-search")

    assert "tavily-search" in entries
    assert tavily is not None
    assert tavily.server["id"] == "tavily"
    assert tavily.server["transport"] == "streamable_http"
    assert tavily.server["search_only_tools"] == ["tavily_search", "tavily_extract"]
    assert sorted(tavily.env_examples) == ["TAVILY_API_KEY"]


def test_console_catalog_reads_mcp_entries_from_component_catalog() -> None:
    from console.control import catalog

    items = {item.id: item for item in catalog.full_catalog(use_cache=False)}
    tavily = items["mcp:tavily-search"]

    assert tavily.name == "Tavily web search MCP"
    assert tavily.infra_service_id == "tavily"
    assert tavily.requires_env == ["TAVILY_API_KEY"]
    assert [tool.name for tool in tavily.tools] == ["tavily_search", "tavily_extract"]


def test_console_inventory_resolves_mcp_refs_from_component_catalog(tmp_path, monkeypatch) -> None:
    from console.control import inventory

    bot_dir = tmp_path / "bot"
    (bot_dir / "mcp").mkdir(parents=True)
    (bot_dir / "mcp" / "servers.yaml").write_text(
        textwrap.dedent(
            """            servers:
              - ref: tavily-search
                enabled: true
            """
        ),
        encoding="utf-8",
    )
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_text(
        textwrap.dedent(
            """            id: test-bot
            display_name: Test Bot
            platform:
              type: qq
            tools:
              packs: []
              mcp:
                servers: mcp/servers.yaml
            agents:
              presets: []
            deploy:
              target: wsl2
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        inventory.services,
        "all_services_status",
        lambda: [{"id": "tavily", "state": "running", "color": "green"}],
    )

    result = inventory.bot_inventory(
        BotInstance(
            instance_id="test-bot",
            bot_spec=str(bot_yaml),
            display_name="Test Bot",
            platform="qq",
        )
    )

    assert result["mcp_services"] == [
        {
            "ref": "tavily-search",
            "title": "Tavily web search MCP",
            "enabled": True,
            "risk": "search",
            "exposure": "subagent",
            "allowed_subagents": [],
            "transport": "streamable_http",
            "infra_service_id": "tavily",
            "infra_state": "running",
            "infra_color": "green",
        }
    ]


def test_console_no_longer_reads_mcp_catalog_yaml_directly() -> None:
    for rel in ("console/control/catalog.py", "console/control/inventory.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "mcp_catalog.yaml" not in text
        assert "chatcopilot.botspec.mcp_catalog" not in text
