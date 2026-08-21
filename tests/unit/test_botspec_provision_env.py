from __future__ import annotations

import os
import shlex
import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from chatcopilot.botspec.cli import main as bot_cli_main
from chatcopilot.core.settings import load_local_env_values


class LocalEnvParsingTests(unittest.TestCase):
    def test_shell_escapes_and_windows_paths_are_both_preserved(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text(
                "export DRIVE=" + r"C:\workspace\data" + "\n"
                "export DRIVE_ESCAPED=" + r"C:\\workspace\\data" + "\n"
                "export UNC=" + r"\\server\share\directory" + "\n"
                "export UNC_ESCAPED=" + r"\\\\server\\share\\directory" + "\n"
                'export QUOTED_UNC="' + r"\\server\share\directory" + '"\n'
                'export QUOTED_UNC_ESCAPED="'
                + r"\\\\server\\share\\directory"
                + '"\n'
                + "export DRIVE_ROOT=C:\\\n"
                + "export DRIVE_TRAILING=C:\\temp\\\n"
                + "export UNC_TRAILING=\\\\server\\share\\\n"
                + 'export QUOTED_DRIVE_TRAILING="C:\\temp\\"\n'
                + 'export QUOTED_UNC_TRAILING="\\\\server\\share\\"\n'
                "export SPACE=hello\\ world\n"
                "export HASH=hello\\#world\n"
                'export QUOTE=hello\\"world\n',
                encoding="utf-8",
            )

            values = load_local_env_values(path)

            self.assertEqual(values["DRIVE"], r"C:\workspace\data")
            self.assertEqual(values["DRIVE_ESCAPED"], r"C:\workspace\data")
            self.assertEqual(values["UNC"], r"\\server\share\directory")
            self.assertEqual(values["UNC_ESCAPED"], r"\\server\share\directory")
            self.assertEqual(values["QUOTED_UNC"], r"\\server\share\directory")
            self.assertEqual(values["QUOTED_UNC_ESCAPED"], r"\\server\share\directory")
            self.assertEqual(values["DRIVE_ROOT"], "C:\\")
            self.assertEqual(values["DRIVE_TRAILING"], "C:\\temp\\")
            self.assertEqual(values["UNC_TRAILING"], "\\\\server\\share\\")
            self.assertEqual(values["QUOTED_DRIVE_TRAILING"], "C:\\temp\\")
            self.assertEqual(values["QUOTED_UNC_TRAILING"], "\\\\server\\share\\")
            self.assertEqual(values["SPACE"], "hello world")
            self.assertEqual(values["HASH"], "hello#world")
            self.assertEqual(values["QUOTE"], 'hello"world')

    def test_parse_errors_do_not_echo_the_source_line_or_value(self) -> None:
        with TemporaryDirectory() as tmp:
            secret = "private-local-env-value"
            path = Path(tmp) / "local.env"
            path.write_text(f"not-an-assignment {secret}\n", encoding="utf-8")

            with self.assertRaises(ValueError) as raised:
                load_local_env_values(path)

            message = str(raised.exception)
            self.assertIn(f"{path}:1", message)
            self.assertIn("缺少 KEY=value", message)
            self.assertNotIn(secret, message)


class BotSpecProvisionEnvTests(unittest.TestCase):
    def test_doctor_reads_default_local_env_without_exposing_values(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_dir = base / "feishu-bot"
            bot_dir.mkdir()
            (bot_dir / "persona.md").write_text("feishu bot\n", encoding="utf-8")
            app_secret = "private-test-secret"
            (bot_dir / "local.env").write_text(
                textwrap.dedent(
                    f"""\
                    export FEISHU_APP_ID="cli_test"
                    export FEISHU_APP_SECRET="{app_secret}"
                    """
                ),
                encoding="utf-8",
            )
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    """\
                    id: feishu-bot
                    display_name: Feishu Bot
                    platform:
                      type: feishu
                      adapter: feishu_acp
                    prompts:
                      schema_version: 2
                      identity: persona.md
                      response_style: persona.md
                    tools:
                      packs: []
                    """
                ),
                encoding="utf-8",
            )

            output = StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {"HOME": str(base)},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                code = bot_cli_main(["doctor", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            self.assertIn("platform.type=feishu 凭据齐全", output.getvalue())
            self.assertNotIn(app_secret, output.getvalue())

    @staticmethod
    def _write_qq_bot(
        base: Path,
        local_env: str,
        *,
        group_require_mention: bool = True,
    ) -> tuple[Path, Path]:
        bot_dir = base / "qq-bot"
        bot_dir.mkdir()
        runtime_env = base / "qq-runtime.env"
        (bot_dir / "persona.md").write_text("qq bot\n", encoding="utf-8")
        (bot_dir / "local.env").write_text(local_env, encoding="utf-8")
        bot_yaml = bot_dir / "bot.yaml"
        bot_yaml.write_text(
            textwrap.dedent(
                f"""\
                id: qq-bot
                display_name: QQ Bot
                platform:
                  type: qq
                  adapter: qq_acp
                prompts:
                  schema_version: 2
                  identity: persona.md
                  response_style: persona.md
                tools:
                  packs: []
                access:
                  group_require_mention: {str(group_require_mention).lower()}
                deploy:
                  target: wsl2
                  instance_id: qq-bot
                  workspace_root: {(base / "workspace").as_posix()}
                  env_file: {runtime_env.as_posix()}
                """
            ),
            encoding="utf-8",
        )
        return bot_yaml, runtime_env

    def test_qq_provision_rejects_weak_token_without_echoing_it(self) -> None:
        with TemporaryDirectory() as tmp:
            weak_token = 'weak"token'
            bot_yaml, runtime_env = self._write_qq_bot(
                Path(tmp),
                textwrap.dedent(
                    f"""\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export QQ_ACCOUNT="10001"
                    export QQ_ACCESS_TOKEN='{weak_token}'
                    """
                ),
            )
            output = StringIO()
            with redirect_stdout(output):
                code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 1)
            self.assertFalse(runtime_env.exists())
            self.assertIn("qq_access_token_invalid", output.getvalue())
            self.assertNotIn(weak_token, output.getvalue())

    def test_qq_provision_rejects_non_loopback_websocket(self) -> None:
        with TemporaryDirectory() as tmp:
            private_host = ".".join(("10", "0", "0", "1"))
            bot_yaml, runtime_env = self._write_qq_bot(
                Path(tmp),
                textwrap.dedent(
                    f"""\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export QQ_ACCOUNT="10001"
                    export QQ_ACCESS_TOKEN="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    export QQ_WS_URL="ws://{private_host}:3001"
                    """
                ),
            )
            output = StringIO()
            with redirect_stdout(output):
                code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 1)
            self.assertFalse(runtime_env.exists())
            self.assertIn("qq_websocket_url_not_loopback", output.getvalue())

    def test_qq_provision_accepts_strong_token_and_loopback_urls(self) -> None:
        with TemporaryDirectory() as tmp:
            token = "b" * 64
            bot_yaml, runtime_env = self._write_qq_bot(
                Path(tmp),
                textwrap.dedent(
                    f"""\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export QQ_ACCOUNT="10001"
                    export QQ_ACCESS_TOKEN="{token}"
                    export QQ_WS_URL="ws://127.0.0.1:3001"
                    export QQ_AT_PROXY_URL="ws://localhost:3002"
                    export QQ_ALLOW_GROUPS="30003"
                    """
                ),
            )

            code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            rendered = runtime_env.read_text(encoding="utf-8")
            self.assertIn(f"export QQ_ACCESS_TOKEN={token}", rendered)
            self.assertIn("export QQ_ALLOW_GROUPS=30003", rendered)

    def test_qq_provision_rejects_invalid_group_allowlist_without_echoing_it(self) -> None:
        with TemporaryDirectory() as tmp:
            private_value = "invalid-private-group"
            bot_yaml, runtime_env = self._write_qq_bot(
                Path(tmp),
                textwrap.dedent(
                    f"""\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export QQ_ACCOUNT="10001"
                    export QQ_ACCESS_TOKEN="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
                    export QQ_ALLOW_GROUPS="{private_value}"
                    """
                ),
            )
            output = StringIO()
            with redirect_stdout(output):
                code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 1)
            self.assertFalse(runtime_env.exists())
            self.assertIn("qq_group_allowlist_invalid", output.getvalue())
            self.assertNotIn(private_value, output.getvalue())

    def test_qq_provision_rejects_group_mention_policy_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_yaml, runtime_env = self._write_qq_bot(
                Path(tmp),
                textwrap.dedent(
                    """\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export QQ_ACCOUNT="10001"
                    export QQ_ACCESS_TOKEN="cccccccccccccccccccccccccccccccc"
                    export QQ_REQUIRE_AT_IN_GROUP="false"
                    """
                ),
            )
            output = StringIO()
            with redirect_stdout(output):
                code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 1)
            self.assertFalse(runtime_env.exists())
            self.assertIn(
                "qq_group_mention_policy_mismatch",
                output.getvalue(),
            )

    def test_qq_provision_accepts_consistent_disabled_group_mention(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_yaml, runtime_env = self._write_qq_bot(
                Path(tmp),
                textwrap.dedent(
                    """\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export QQ_ACCOUNT="10001"
                    export QQ_ACCESS_TOKEN="dddddddddddddddddddddddddddddddd"
                    export QQ_REQUIRE_AT_IN_GROUP="false"
                    """
                ),
                group_require_mention=False,
            )

            code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            self.assertTrue(runtime_env.exists())

    def test_provision_env_expands_only_leading_home_path_markers(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_dir = base / "home-path-bot"
            bot_dir.mkdir()
            runtime_env = base / "runtime.env"
            (bot_dir / "persona.md").write_text("home path bot\n", encoding="utf-8")
            (bot_dir / "local.env").write_text(
                textwrap.dedent(
                    """\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export FEISHU_APP_ID="cli_test"
                    export FEISHU_APP_SECRET="secret"
                    export CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT="$HOME/ChatCopilot"
                    export CHATCOPILOT_CODEBASE_CACHE_ROOT="${HOME}/.cache/codebases"
                    export CHATCOPILOT_WIKI_ROOT="~/wiki"
                    export CHATCOPILOT_TEST_HOME_ONLY="$HOME"
                    export CHATCOPILOT_TEST_BRACED_HOME_ONLY="${HOME}"
                    export CHATCOPILOT_TEST_TILDE_ONLY="~"
                    export CHATCOPILOT_TEST_EMBEDDED="prefix/$HOME"
                    export CHATCOPILOT_TEST_OTHER="$OTHER/repo"
                    """
                ),
                encoding="utf-8",
            )
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    f"""\
                    id: home-path-bot
                    display_name: Home Path Bot
                    platform:
                      type: feishu
                      adapter: feishu_acp
                    prompts:
                      schema_version: 2
                      identity: persona.md
                      response_style: persona.md
                    tools:
                      packs: []
                    deploy:
                      target: wsl2
                      instance_id: home-path-bot
                      workspace_root: {(base / "workspace").as_posix()}
                      env_file: {runtime_env.as_posix()}
                    """
                ),
                encoding="utf-8",
            )

            code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            content = runtime_env.read_text(encoding="utf-8")
            home = Path("~").expanduser()
            expected = {
                "CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT": home / "ChatCopilot",
                "CHATCOPILOT_CODEBASE_CACHE_ROOT": home / ".cache" / "codebases",
                "CHATCOPILOT_WIKI_ROOT": home / "wiki",
                "CHATCOPILOT_TEST_HOME_ONLY": home,
                "CHATCOPILOT_TEST_BRACED_HOME_ONLY": home,
                "CHATCOPILOT_TEST_TILDE_ONLY": home,
            }
            for key, path in expected.items():
                self.assertIn(f"export {key}={shlex.quote(str(path))}", content)
            self.assertIn("export CHATCOPILOT_TEST_EMBEDDED='prefix/$HOME'", content)
            self.assertIn("export CHATCOPILOT_TEST_OTHER='$OTHER/repo'", content)

    def test_provision_env_carries_tavily_api_key(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_dir = base / "test-bot"
            bot_dir.mkdir()
            (bot_dir / "mcp").mkdir()
            runtime_env = base / "runtime.env"
            (bot_dir / "persona.md").write_text("test bot\n", encoding="utf-8")
            (bot_dir / "mcp" / "servers.yaml").write_text(
                textwrap.dedent(
                    """\
                    servers:
                      - ref: github-readonly
                        enabled: true
                    """
                ),
                encoding="utf-8",
            )
            (bot_dir / "local.env").write_text(
                textwrap.dedent(
                    """\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export FEISHU_APP_ID="cli_test"
                    export FEISHU_APP_SECRET="secret"
                    export TAVILY_API_KEY="tvly-test"
                    export GITHUB_MCP_AUTHORIZATION="Bearer github-test"
                    """
                ),
                encoding="utf-8",
            )
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    f"""\
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
                      instance_id: test-bot
                      workspace_root: {(base / "workspace").as_posix()}
                      env_file: {runtime_env.as_posix()}
                    """
                ),
                encoding="utf-8",
            )

            code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            content = runtime_env.read_text(encoding="utf-8")
            self.assertIn("export CHATCOPILOT_SOURCE_BOT_SPEC=", content)
            self.assertIn("test-bot", content)
            self.assertIn("bot.yaml", content)
            self.assertIn("export TAVILY_API_KEY=tvly-test", content)
            self.assertIn("export GITHUB_MCP_AUTHORIZATION='Bearer github-test'", content)
            self.assertEqual(content.count("export GITHUB_MCP_AUTHORIZATION="), 1)

    def test_local_env_can_override_tool_pack_runtime_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_dir = base / "sample-bot"
            bot_dir.mkdir()
            runtime_env = base / "runtime.env"
            (bot_dir / "persona.md").write_text("sample bot\n", encoding="utf-8")
            (bot_dir / "local.env").write_text(
                textwrap.dedent(
                    """\
                    export CHATCOPILOT_CHAT_API_KEY="sk-test"
                    export FEISHU_APP_ID="cli_test"
                    export FEISHU_APP_SECRET="secret"
                    export CHATCOPILOT_HTTP_ROUTE_MODULES="custom.routes"
                    """
                ),
                encoding="utf-8",
            )
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    f"""\
                    id: sample-bot
                    display_name: Sample Bot
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
                    deploy:
                      target: wsl2
                      instance_id: sample-bot
                      workspace_root: {(base / "workspace").as_posix()}
                      env_file: {runtime_env.as_posix()}
                    """
                ),
                encoding="utf-8",
            )

            code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            content = runtime_env.read_text(encoding="utf-8")
            self.assertIn("export CHATCOPILOT_HTTP_ROUTE_MODULES=custom.routes", content)

    def test_provision_env_renders_botspec_llm_defaults_and_keeps_local_override(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_dir = base / "route-bot"
            bot_dir.mkdir()
            runtime_env = base / "runtime.env"
            (bot_dir / "persona.md").write_text("route bot\n", encoding="utf-8")
            (bot_dir / "local.env").write_text(
                textwrap.dedent(
                    """\
                    export CHATCOPILOT_ROUTE_API_KEY="sk-test"
                    export CHATCOPILOT_ROUTE_CODE_MODEL="local-code-model"
                    export CHATCOPILOT_ROUTE_CODE_TASK_PROFILE="sol-max"
                    export FEISHU_APP_ID="cli_test"
                    export FEISHU_APP_SECRET="secret"
                    """
                ),
                encoding="utf-8",
            )
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text(
                textwrap.dedent(
                    f"""\
                    id: route-bot
                    display_name: Route Bot
                    platform:
                      type: feishu
                      adapter: feishu_acp
                    llm:
                      chat:
                        env_prefix: CHATCOPILOT_ROUTE
                      research:
                        env_prefix: CHATCOPILOT_ROUTE_RESEARCH
                        execution: agent
                        prefixes: [/research, /调研]
                        web_search: live
                      code:
                        enabled: true
                        model: botspec-code-model
                        reasoning_effort: medium
                        profiles:
                          sol-high:
                            model: gpt-5.6-sol
                            reasoning_effort: high
                          sol-max:
                            model: gpt-5.6-sol
                            reasoning_effort: max
                        code_task_profile: sol-high
                        allowed_roles: [owner]
                    prompts:
                      schema_version: 2
                      identity: persona.md
                      response_style: persona.md
                    tools:
                      packs: []
                    deploy:
                      target: wsl2
                      instance_id: route-bot
                      workspace_root: {(base / "workspace").as_posix()}
                      env_file: {runtime_env.as_posix()}
                    """
                ),
                encoding="utf-8",
            )

            code = bot_cli_main(["provision-env", "--bot", str(bot_yaml)])

            self.assertEqual(code, 0)
            content = runtime_env.read_text(encoding="utf-8")
            self.assertIn("export CHATCOPILOT_ROUTE_ROUTER_ENABLED=false", content)
            self.assertIn(
                "export CHATCOPILOT_ROUTE_RESEARCH_EXECUTION=agent",
                content,
            )
            self.assertIn(
                "export CHATCOPILOT_ROUTE_RESEARCH_PREFIXES='/research,/调研'",
                content,
            )
            self.assertIn(
                "export CHATCOPILOT_ROUTE_RESEARCH_WEB_SEARCH=live",
                content,
            )
            self.assertIn("export CHATCOPILOT_ROUTE_CODE_MODEL=local-code-model", content)
            self.assertIn(
                "export CHATCOPILOT_ROUTE_CODE_REASONING_EFFORT=medium",
                content,
            )
            self.assertIn("CHATCOPILOT_ROUTE_CODE_PROFILES_JSON=", content)
            self.assertIn("sol-high", content)
            self.assertIn(
                "export CHATCOPILOT_ROUTE_CODE_TASK_PROFILE=sol-max",
                content,
            )
            self.assertIn("export CHATCOPILOT_ROUTE_CODE_ALLOWED_ROLES=owner", content)


if __name__ == "__main__":
    unittest.main()
