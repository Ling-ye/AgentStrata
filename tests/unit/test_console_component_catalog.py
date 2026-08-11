from __future__ import annotations

import textwrap
from pathlib import Path

from console.control.instances import BotInstance


ROOT = Path(__file__).resolve().parents[2]


def test_component_catalog_exposes_mcp_entries() -> None:
    from chatcopilot.component_catalog import get_mcp_catalog_entry, iter_mcp_catalog_entries

    entries = dict(iter_mcp_catalog_entries())
    playwright = get_mcp_catalog_entry("playwright-browser")

    assert {
        "tavily-search",
        "brave-search",
        "searxng-search",
        "sequential-thinking",
        "taoke-shopping",
    }.isdisjoint(entries)
    assert {
        "github-readonly",
        "xiaohongshu-search",
        "playwright-browser",
        "git-local",
    }.issubset(entries)
    assert playwright is not None
    assert playwright.server["id"] == "playwright"
    assert playwright.server["transport"] == "streamable_http"
    assert playwright.server["allowed_subagents"] == ["browser_reader"]
    assert playwright.env_examples == {}


def test_console_catalog_reads_mcp_entries_from_component_catalog() -> None:
    from console.control import catalog

    items = {item.id: item for item in catalog.full_catalog(use_cache=False)}
    playwright = items["mcp:playwright-browser"]

    assert "mcp:tavily-search" not in items
    assert "mcp:sequential-thinking" not in items
    assert "mcp:taoke-shopping" not in items
    assert playwright.name == "Playwright dynamic webpage reader"
    assert playwright.infra_service_id == "playwright"
    assert playwright.requires_env == []
    assert playwright.tools == []


def test_console_catalog_projects_builtin_and_shared_module_tools_exactly() -> None:
    from console.control import catalog

    items = {item.id: item for item in catalog.full_catalog(use_cache=False)}

    workspace = items["tool_pack:workspace.read_write"]
    document = items["tool_pack:feishu.document"]
    assert workspace.has_tools
    assert {tool.name for tool in workspace.tools} >= {
        "list_workspace",
        "get_task_status",
    }
    assert {tool.name for tool in document.tools} == {
        "feishu_doc_create",
        "feishu_doc_append",
        "feishu_api_get",
    }


def test_console_inventory_resolves_mcp_refs_from_component_catalog(tmp_path, monkeypatch) -> None:
    from console.control import inventory

    bot_dir = tmp_path / "bot"
    (bot_dir / "mcp").mkdir(parents=True)
    (bot_dir / "mcp" / "servers.yaml").write_text(
        textwrap.dedent(
            """            servers:
              - ref: playwright-browser
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
        lambda: [{"id": "playwright", "state": "running", "color": "green"}],
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
            "ref": "playwright-browser",
            "title": "Playwright dynamic webpage reader",
            "enabled": True,
            "risk": "interactive",
            "exposure": "subagent",
            "allowed_subagents": ["browser_reader"],
            "transport": "streamable_http",
            "infra_service_id": "playwright",
            "infra_state": "running",
            "infra_color": "green",
        }
    ]


def test_console_no_longer_reads_mcp_catalog_yaml_directly() -> None:
    for rel in ("console/control/catalog.py", "console/control/inventory.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "mcp_catalog.yaml" not in text
        assert "chatcopilot.botspec.mcp_catalog" not in text


def test_console_does_not_own_tool_module_import_logic() -> None:
    text = (ROOT / "console/control/catalog.py").read_text(encoding="utf-8")

    assert "import importlib" not in text
    assert "_collect_tools_from_module" not in text
    assert "iter_tool_pack_tools" in text
