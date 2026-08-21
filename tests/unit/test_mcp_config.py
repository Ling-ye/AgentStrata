from __future__ import annotations

import os
import shutil
import textwrap
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.mcp import load_mcp_server_configs


@contextmanager
def _scratch_dir():
    root = Path(__file__).resolve().parents[2] / "scratch_unit_tests" / f"mcp-config-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write_bot(base: Path, servers_yaml: str) -> Path:
    bot_dir = base / "test-bot"
    (bot_dir / "mcp").mkdir(parents=True)
    (bot_dir / "persona.md").write_text("test bot\n", encoding="utf-8")
    (bot_dir / "mcp" / "servers.yaml").write_text(servers_yaml, encoding="utf-8")
    (bot_dir / "bot.yaml").write_text(
        textwrap.dedent(
            """\
            id: test-bot
            display_name: Test Bot
            platform:
              type: feishu
              adapter: feishu_acp
            prompts:
              schema_version: 2
              identity: persona.md
              response_style: persona.md
            tools:
              packs:
                - workspace.read_write
              mcp:
                servers: mcp/servers.yaml
            deploy:
              target: wsl2
            """
        ),
        encoding="utf-8",
    )
    return bot_dir / "bot.yaml"


class McpConfigTests(unittest.TestCase):
    def test_rejects_bot_authored_catalog_provenance(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: custom-browser
                            catalog_ref: playwright-browser
                            enabled: true
                            transport: streamable_http
                            url: http://127.0.0.1:19066/mcp
                            exposure: subagent
                            risk: interactive
                        """
                    ),
                )
            )

            messages = [issue.message for issue in validate_botspec(spec)]

            self.assertTrue(any("catalog_ref 是 runtime 保留字段" in item for item in messages))
            self.assertEqual(load_mcp_server_configs(spec)[0].catalog_ref, "")

    def test_loads_enabled_stdio_server_with_resolved_env(self) -> None:
        with _scratch_dir() as tmp:
            os.environ["TEST_MCP_KEY"] = "secret-value"
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: web-search
                            enabled: true
                            transport: stdio
                            command: python
                            args: ["server.py"]
                            artifact_digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
                            env:
                              TEST_MCP_KEY: "${TEST_MCP_KEY}"
                            tool_prefix: web_
                            exposure: subagent
                            allowed_subagents: ["web_research"]
                            allowed_tools: [search, extract]
                            denied_tools: [admin]
                            risk: search
                            timeout_seconds: 12
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].id, "web-search")
            self.assertEqual(configs[0].env["TEST_MCP_KEY"], "secret-value")
            self.assertEqual(
                configs[0].artifact_digest,
                "sha256:" + "0" * 64,
            )
            self.assertEqual(configs[0].tool_prefix, "web_")
            self.assertEqual(configs[0].exposure, "subagent")
            self.assertEqual(configs[0].allowed_subagents, ("web_research",))
            self.assertEqual(configs[0].allowed_tools, ("search", "extract"))
            self.assertEqual(configs[0].denied_tools, ("admin",))
            self.assertEqual(configs[0].risk, "search")
            self.assertEqual(configs[0].timeout_seconds, 12)

    def test_loads_catalog_ref_with_bot_override(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - ref: playwright-browser
                            enabled: true
                            timeout_seconds: 7
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].id, "playwright")
            self.assertEqual(configs[0].catalog_ref, "playwright-browser")
            self.assertEqual(configs[0].transport, "streamable_http")
            self.assertEqual(configs[0].url, "http://localhost:18066/mcp")
            self.assertEqual(configs[0].risk, "interactive")
            self.assertEqual(configs[0].allowed_subagents, ("browser_reader",))
            self.assertEqual(configs[0].timeout_seconds, 7)

    def test_loads_xiaohongshu_catalog_ref(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - ref: xiaohongshu-search
                            enabled: true
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].id, "xiaohongshu")
            self.assertEqual(configs[0].catalog_ref, "xiaohongshu-search")
            self.assertEqual(configs[0].transport, "streamable_http")
            self.assertEqual(configs[0].url, "http://localhost:18060/mcp")
            self.assertEqual(configs[0].tool_prefix, "xhs_")
            self.assertEqual(configs[0].risk, "search")
            self.assertEqual(configs[0].search_only_tools, ("search_feeds",))
            self.assertEqual(configs[0].max_concurrency, 1)

    def test_loads_playwright_interactive_catalog_ref(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - ref: playwright-browser
                            enabled: true
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].id, "playwright")
            self.assertEqual(configs[0].risk, "interactive")
            self.assertEqual(configs[0].allowed_subagents, ("browser_reader",))
            self.assertEqual(configs[0].max_concurrency, 1)

    def test_loads_search_domain_preferences(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: focused-search
                            enabled: true
                            transport: streamable_http
                            url: http://localhost:18070/mcp
                            risk: search
                            preferred_domains: [docs.example.com]
                            excluded_domains: [spam.example.com]
                            search_domain_guidance: Prefer official documentation.
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(configs[0].preferred_domains, ("docs.example.com",))
            self.assertEqual(configs[0].excluded_domains, ("spam.example.com",))
            self.assertEqual(
                configs[0].search_domain_guidance,
                "Prefer official documentation.",
            )

    def test_loads_readonly_server_with_explicit_mcp_query_subagent(self) -> None:
        with _scratch_dir() as tmp:
            os.environ["GITHUB_MCP_AUTHORIZATION"] = "Bearer token"
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: github
                            enabled: true
                            transport: streamable_http
                            url: https://api.githubcopilot.com/mcp/
                            headers:
                              Authorization: "${GITHUB_MCP_AUTHORIZATION}"
                            tool_prefix: github_
                            exposure: subagent
                            allowed_subagents: ["mcp_query"]
                            risk: readonly
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].headers["Authorization"], "Bearer token")
            self.assertEqual(configs[0].allowed_subagents, ("mcp_query",))
            self.assertEqual(configs[0].risk, "readonly")

    def test_validate_rejects_invalid_exposure_risk_and_subagent_list(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: bad
                            transport: stdio
                            command: python
                            exposure: everywhere
                            risk: dangerous
                            allowed_subagents: web_research
                        """
                    ),
                )
            )

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
            messages = "\n".join(issue.message for issue in errors)

            self.assertIn("MCP exposure", messages)
            self.assertIn("MCP risk", messages)
            self.assertIn("allowed_subagents", messages)

    def test_validate_rejects_invalid_tool_policy_lists(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: bad-tools
                            transport: stdio
                            command: python
                            allowed_tools: search
                            denied_tools: [""]
                        """
                    ),
                )
            )

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
            messages = "\n".join(issue.message for issue in errors)

            self.assertIn("allowed_tools", messages)
            self.assertIn("denied_tools", messages)

    def test_validate_rejects_unknown_catalog_ref(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - ref: missing-server
                            enabled: true
                        """
                    ),
                )
            )

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

            self.assertTrue(any("未知 MCP catalog ref" in issue.message for issue in errors))

    def test_validate_rejects_plaintext_env_secret(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: web-search
                            transport: stdio
                            command: python
                            env:
                              API_KEY: plaintext
                        """
                    ),
                )
            )

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

            self.assertTrue(any("不能写明文 secret" in issue.message for issue in errors))

    def test_loads_max_concurrency_from_yaml(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: browser-svc
                            enabled: true
                            transport: streamable_http
                            url: http://localhost:18060/mcp
                            max_concurrency: 1
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].max_concurrency, 1)

    def test_max_concurrency_defaults_to_zero(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: stateless-svc
                            enabled: true
                            transport: streamable_http
                            url: http://localhost:18061/mcp
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].max_concurrency, 0)

    def test_validate_rejects_negative_max_concurrency(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: bad-concurrency
                            transport: streamable_http
                            url: http://localhost:18060/mcp
                            max_concurrency: -1
                        """
                    ),
                )
            )

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

            self.assertTrue(any("max_concurrency" in issue.message for issue in errors))

    def test_validate_rejects_invalid_search_domain_lists(self) -> None:
        with _scratch_dir() as tmp:
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: bad-domains
                            transport: streamable_http
                            url: http://localhost:18060/mcp
                            preferred_domains: docs.example.com
                            excluded_domains: [""]
                        """
                    ),
                )
            )

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
            fields = {issue.field for issue in errors}

            self.assertIn("mcp.servers[0].preferred_domains", fields)
            self.assertIn("mcp.servers[0].excluded_domains", fields)

    def test_runtime_skips_server_when_env_ref_is_missing(self) -> None:
        with _scratch_dir() as tmp:
            os.environ.pop("MISSING_MCP_KEY", None)
            spec = load_botspec(
                _write_bot(
                    tmp,
                    textwrap.dedent(
                        """\
                        servers:
                          - id: web-search
                            transport: stdio
                            command: python
                            env:
                              API_KEY: "${MISSING_MCP_KEY}"
                        """
                    ),
                )
            )

            configs = load_mcp_server_configs(spec)

            self.assertEqual(configs, ())


if __name__ == "__main__":
    unittest.main()
