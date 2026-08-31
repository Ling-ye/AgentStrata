from __future__ import annotations

import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from chatcopilot.botspec.cli import main as bot_cli_main
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.core.config import LLMConfig, load_config, load_llm_profile

_REPO_ROOT = Path(__file__).resolve().parents[2]


class LlmRuntimeConfigTests(unittest.TestCase):
    def test_codex_command_configuration_keeps_safe_defaults(self) -> None:
        config = load_config(
            Path("/tmp/chatcopilot-missing-routing.yaml"),
            env_prefix="CHATCOPILOT_ROUTETEST",
        )

        self.assertFalse(config.routing.enabled)
        self.assertEqual(config.routing.code_provider, "codex_cli")
        self.assertEqual(
            config.routing.code_command,
            "codex exec --model {model} --cd {workdir}",
        )

    def test_env_parses_codex_runtime_configuration(self) -> None:
        prefix = "CHATCOPILOT_ROUTETEST"
        env = {
            prefix + "_CODE_MODEL": "gpt-route-test",
            prefix + "_CODE_REASONING_EFFORT": "high",
            prefix + "_CODE_PROFILES_JSON": (
                '{"sol-high":{"model":"gpt-5.6-sol","reasoning_effort":"high"}}'
            ),
            prefix + "_CODE_TASK_PROFILE": "sol-high",
            prefix + "_CODE_COMMAND": "codex exec --model {model} --cwd {workdir}",
            prefix + "_CODE_WORKDIR_ENV": "CHATCOPILOT_TEST_ROOT",
            prefix + "_CODE_TIMEOUT_SECONDS": "17",
            prefix + "_CODE_ALLOWED_ROLES": "owner,admin",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            config = load_config(
                Path("/tmp/chatcopilot-missing-routing.yaml"),
                env_prefix=prefix,
            )

        self.assertEqual(config.routing.code_model, "gpt-route-test")
        self.assertEqual(config.routing.code_reasoning_effort, "high")
        self.assertEqual(config.routing.code_profiles["sol-high"].model, "gpt-5.6-sol")
        self.assertEqual(config.routing.code_task_profile, "sol-high")
        self.assertEqual(config.routing.code_workdir_env, "CHATCOPILOT_TEST_ROOT")
        self.assertEqual(config.routing.code_timeout_seconds, 17)
        self.assertEqual(config.routing.code_allowed_roles, ("owner", "admin"))

    def test_invalid_codex_runtime_configuration_fails_visibly(self) -> None:
        prefix = "CHATCOPILOT_ROUTEINVALID"
        for suffix, value in (
            ("_CODE_PROVIDER", "unknown"),
            ("_CODE_TIMEOUT_SECONDS", "0"),
            ("_CODE_REASONING_EFFORT", "impossible"),
            ("_CODE_TASK_PROFILE", "missing-profile"),
        ):
            with self.subTest(suffix=suffix), mock.patch.dict(
                os.environ, {prefix + suffix: value}, clear=False
            ):
                with self.assertRaises(ValueError):
                    load_config(
                        Path("/tmp/chatcopilot-missing-routing.yaml"),
                        env_prefix=prefix,
                    )

    def test_research_profile_can_override_only_model(self) -> None:
        main = LLMConfig(
            base_url="https://chat.example/v1",
            model="chat-model",
            api_key="sk-chat",
            timeout=120,
        )
        with mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_PROFILE_RESEARCH_MODEL": "research-model"},
            clear=False,
        ):
            research = load_llm_profile("CHATCOPILOT_PROFILE_RESEARCH", fallback=main)

        self.assertEqual(research.model, "research-model")
        self.assertEqual(research.base_url, main.base_url)
        self.assertEqual(research.api_key, main.api_key)


class LingyeDirectCodexConfigTests(unittest.TestCase):
    def test_lingye_uses_direct_codex_with_inprocess_search_providers(self) -> None:
        spec = load_botspec(_REPO_ROOT / "bots/lingye-copilot-qq/bot.yaml")

        self.assertEqual(spec.agents.backend, "codex")
        self.assertEqual(spec.agents.codex.owner_access, "worktree")
        self.assertEqual(spec.agents.codex.member_access, "workspace")
        self.assertEqual(spec.agents.include, ())
        self.assertTrue(spec.agents.research_enabled)
        self.assertEqual(
            [provider.kind for provider in spec.agents.search_providers],
            ["tavily", "brave", "searxng"],
        )
        self.assertEqual(
            [provider.kind for provider in spec.agents.search_providers if provider.enabled],
            ["tavily", "searxng"],
        )
        self.assertIsNone(spec.context.codebases.registry)
        self.assertNotIn("web.fetch", spec.tools.packs)
        self.assertNotIn("codebase.read", spec.tools.packs)
        self.assertNotIn("codebase.change", spec.tools.packs)
        self.assertIn("dev.code_tasks", spec.tools.packs)
        self.assertNotIn("dev.files", spec.tools.packs)
        self.assertEqual(spec.llm.code.allowed_roles, ("owner",))
        self.assertEqual(spec.llm.code.model, "gpt-5.6-terra")
        self.assertEqual(spec.llm.code.reasoning_effort, "medium")
        self.assertEqual(spec.llm.code.code_task_profile, "sol-max")
        self.assertEqual(
            spec.llm.code.profiles["sol-max"].reasoning_effort,
            "max",
        )

    def test_route_explain_reports_instance_backend_without_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_dir = Path(tmp) / "route-demo"
            bot_dir.mkdir()
            (bot_dir / "persona.md").write_text("route demo\n", encoding="utf-8")
            (bot_dir / "bot.yaml").write_text(
                "\n".join(
                    [
                        "id: route-demo",
                        "display_name: Route Demo",
                        "platform:",
                        "  type: feishu",
                        "  adapter: feishu_acp",
                        "llm:",
                        "  chat:",
                        "    env_prefix: CHATCOPILOT_ROUTEDEMO",
                        "  code:",
                        "    enabled: true",
                        "    model: code-from-botspec",
                        "    reasoning_effort: medium",
                        "    profiles:",
                        "      sol-max:",
                        "        model: gpt-5.6-sol",
                        "        reasoning_effort: max",
                        "    code_task_profile: sol-max",
                        "prompts:",
                        "  schema_version: 2",
                        "  identity: persona.md",
                        "  response_style: persona.md",
                        "tools:",
                        "  packs: []",
                        "agents:",
                        "  backend: codex",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (bot_dir / "local.env").write_text(
                "export CHATCOPILOT_ROUTEDEMO_API_KEY=sk-secret\n"
                "export CHATCOPILOT_ROUTEDEMO_MODEL=chat-model\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = bot_cli_main(
                    ["route-explain", "--bot", str(bot_dir / "bot.yaml"), "status"]
                )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("backend=codex", rendered)
        self.assertIn("selection_scope=instance", rendered)
        self.assertIn("cross_backend_routing=false", rendered)
        self.assertIn("main.model=code-from-botspec", rendered)
        self.assertIn("main.reasoning_effort=medium", rendered)
        self.assertIn("code_task.profile=sol-max", rendered)
        self.assertIn("code_task.model=gpt-5.6-sol", rendered)
        self.assertIn("code_task.reasoning_effort=max", rendered)
        self.assertNotIn("sk-secret", rendered)


if __name__ == "__main__":
    unittest.main()
