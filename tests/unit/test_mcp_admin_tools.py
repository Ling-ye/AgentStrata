from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest import mock

import yaml
import pytest

from chatcopilot.agent.tools.builtin.mcp_tools import TOOLS
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.core.mcp_probe import (
    McpProbeResult,
    McpProbeTool,
    _minimal_probe_env,
    probe_mcp_server,
)
from chatcopilot.external_tools.mcp_admin import tools as mcp_admin
from chatcopilot.botspec.mcp import McpServerConfig


def _write_bot(tmp_path: Path) -> Path:
    bot_dir = tmp_path / "test-bot"
    (bot_dir / "prompts").mkdir(parents=True)
    (bot_dir / "mcp").mkdir()
    (bot_dir / "skills").mkdir()
    (bot_dir / "prompts" / "persona.md").write_text("test bot\n", encoding="utf-8")
    (bot_dir / "mcp" / "servers.yaml").write_text("servers: []\n", encoding="utf-8")
    (bot_dir / "skills" / "manifest.yaml").write_text("skills: []\n", encoding="utf-8")
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_text(
        textwrap.dedent(
            """\
            id: test-bot
            display_name: Test Bot
            platform:
              type: feishu
              adapter: feishu_acp
            prompts:
              persona: prompts/persona.md
            tools:
              packs:
                - mcp.admin
              mcp:
                servers: mcp/servers.yaml
            context:
              playbooks:
                manifest: skills/manifest.yaml
            deploy:
              target: wsl2
            """
        ),
        encoding="utf-8",
    )
    return bot_yaml


def _executor() -> ToolExecutor:
    return ToolExecutor(tools=list(TOOLS))


def test_discover_and_approve_playwright_curated_proposal(tmp_path: Path) -> None:
    bot_yaml = _write_bot(tmp_path)

    empty_registry = mcp_admin._RegistryDiscoveryResult(pagination_exhausted=True)
    with mock.patch.object(mcp_admin, "_registry_matches", return_value=empty_registry):
        discovered = _executor().execute("discover_mcp_server", {"query": "Playwright"})
    payload = json.loads(discovered.summary)
    ids = {item["proposal_id"] for item in payload["proposals"]}

    assert "playwright-browser" in ids

    approved = _executor().execute(
        "approve_mcp_server",
        {"proposal_id": "playwright-browser", "bot": str(bot_yaml)},
    )
    result = json.loads(approved.summary)
    data = yaml.safe_load((bot_yaml.parent / "mcp" / "servers.yaml").read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert data["servers"] == [{"ref": "playwright-browser", "enabled": True}]


def test_manual_approval_requires_reviewed_catalog_entry(tmp_path: Path) -> None:
    bot_yaml = _write_bot(tmp_path)

    result = _executor().execute(
        "approve_mcp_server",
        {
            "proposal_id": "manual-test",
            "bot": str(bot_yaml),
            "server": {
                "id": "unsafe",
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer plaintext"},
                "risk": "readonly",
            },
        },
    )

    assert result.ok is True
    payload = json.loads(result.summary)
    assert payload["ok"] is False
    assert payload["error"] == "catalog_entry_required"


def test_approval_prefers_source_bot_spec_env(tmp_path: Path, monkeypatch) -> None:
    source_bot = _write_bot(tmp_path / "source")
    runtime_bot = _write_bot(tmp_path / "runtime")
    monkeypatch.setenv("CHATCOPILOT_SOURCE_BOT_SPEC", str(source_bot))
    monkeypatch.setenv("CHATCOPILOT_BOT_SPEC", str(runtime_bot))

    approved = _executor().execute(
        "approve_mcp_server",
        {"proposal_id": "playwright-browser"},
    )
    result = json.loads(approved.summary)
    source_data = yaml.safe_load((source_bot.parent / "mcp" / "servers.yaml").read_text(encoding="utf-8"))
    runtime_data = yaml.safe_load((runtime_bot.parent / "mcp" / "servers.yaml").read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert [item["ref"] for item in source_data["servers"]] == ["playwright-browser"]
    assert runtime_data["servers"] == []


def test_list_mcp_servers_resolves_catalog_binding(tmp_path: Path) -> None:
    bot_yaml = _write_bot(tmp_path)
    (bot_yaml.parent / "mcp" / "servers.yaml").write_text(
        textwrap.dedent(
            """\
            servers:
              - ref: playwright-browser
                enabled: true
            """
        ),
        encoding="utf-8",
    )

    listed = _executor().execute("list_mcp_servers", {"bot": str(bot_yaml)})
    payload = json.loads(listed.summary)

    assert payload["servers"] == [
        {
            "ref": "playwright-browser",
            "id": "playwright",
            "enabled": True,
            "transport": "streamable_http",
            "exposure": "subagent",
            "allowed_subagents": ["browser_reader"],
            "allowed_tools": [],
            "denied_tools": [],
            "risk": "interactive",
            "tool_prefix": "",
        }
    ]


def test_probe_existing_binding_is_read_only_and_resolves_secret_refs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bot_yaml = _write_bot(tmp_path)
    servers_path = bot_yaml.parent / "mcp" / "servers.yaml"
    servers_path.write_text(
        textwrap.dedent(
            """\
            servers:
              - id: local-docs
                enabled: false
                transport: streamable_http
                url: http://127.0.0.1:8123/mcp
                headers:
                  Authorization: ${DOCS_AUTH}
                risk: readonly
                allowed_tools: [read_docs]
            """
        ),
        encoding="utf-8",
    )
    before = servers_path.read_text(encoding="utf-8")
    monkeypatch.setenv("DOCS_AUTH", "probe-secret")
    probe = McpProbeResult(
        ok=True,
        server_id="local-docs",
        transport="streamable_http",
        server_name="docs",
        server_version="1.0",
        tools=(McpProbeTool("read_docs", "a", "b"),),
    )

    with mock.patch.object(mcp_admin, "probe_mcp_server", return_value=probe) as called:
        completed = _executor().execute(
            "probe_mcp_server",
            {"server_id": "local-docs", "bot": str(bot_yaml)},
        )

    payload = json.loads(completed.summary)
    config = called.call_args.args[0]
    assert called.call_args.kwargs == {"allow_private_network": True}
    assert config.id == "local-docs"
    assert config.headers == {"Authorization": "probe-secret"}
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["binding"] == "id:local-docs"
    assert "probe-secret" not in completed.summary
    assert servers_path.read_text(encoding="utf-8") == before


def test_registry_v01_discovery_follows_cursor_and_filters_deleted(monkeypatch) -> None:
    pages = [
        {
            "servers": [
                {
                    "server": {
                        "name": "example/unrelated",
                        "title": "Other",
                        "version": "1.0.0",
                    },
                    "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
                }
            ],
            "metadata": {"nextCursor": "cursor-1"},
        },
        {
            "servers": [
                {
                    "server": {
                        "name": "io.github.example/docs",
                        "title": "Example Docs",
                        "description": "Read docs",
                        "version": "2.0.0",
                        "repository": {"url": "https://github.com/example/docs"},
                    },
                    "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
                },
                {
                    "server": {
                        "name": "io.github.bad/docs",
                        "title": "Deleted Docs",
                        "status": "deleted",
                    },
                    "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
                },
            ],
            "metadata": {},
        },
    ]
    urls: list[str] = []

    def fetch(url: str):
        urls.append(url)
        return pages.pop(0)

    monkeypatch.setattr(mcp_admin, "_fetch_registry_page", fetch)
    result = mcp_admin._registry_matches("Example Docs", max_pages=5)

    assert result.error_code == ""
    assert result.pages_fetched == 2
    assert result.pagination_exhausted is True
    assert [item["title"] for item in result.proposals] == ["Example Docs"]
    assert "/v0.1/servers?" in urls[0]
    assert "cursor=cursor-1" in urls[1]


def test_registry_failure_is_structured_and_visible(monkeypatch) -> None:
    def fail(_url: str):
        raise TimeoutError("registry timeout")

    monkeypatch.setattr(mcp_admin, "_fetch_registry_page", fail)
    result = mcp_admin._registry_matches("docs", max_pages=2)

    assert result.error_code == "registry_unavailable"
    assert "registry timeout" in result.error


def test_probe_rejects_private_remote_and_does_not_inherit_undeclared_secret(monkeypatch) -> None:
    monkeypatch.setenv("UNDECLARED_PROBE_SECRET", "must-not-leak")
    env = _minimal_probe_env({"DECLARED": "ok"}, home="/tmp/probe-home")
    result = probe_mcp_server(
        McpServerConfig(
            id="private",
            transport="streamable_http",
            url="http://127.0.0.1:8123/mcp",
        )
    )

    assert env["DECLARED"] == "ok"
    assert "UNDECLARED_PROBE_SECRET" not in env
    assert result.ok is False
    assert "requires HTTPS" in result.error
    with pytest.raises(ValueError, match="isolation variables"):
        _minimal_probe_env({"PATH": "/untrusted"}, home="/tmp/probe-home")
