from __future__ import annotations

import os
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from chatcopilot.botspec.cli import main as bot_cli_main
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.core.config import LLMConfig, load_config, load_llm_profile

_REPO_ROOT = Path(__file__).resolve().parents[2]


class LlmRuntimeConfigTests(unittest.TestCase):
    def test_retired_runtime_overrides_do_not_change_effective_configuration(self) -> None:
        prefix = "CHATCOPILOT_RETIREDTEST"
        path = Path("/tmp/chatcopilot-missing-retired.yaml")
        baseline = load_config(path, env_prefix=prefix)
        obsolete = {
            "AUTO_MODE": "auto", "STREAM": "false", "ROUTER_ENABLED": "true",
            "ROUTER_MODE": "invalid", "ROUTER_DEFAULT_ROUTE": "code",
            "ROUTER_CODE_PREFIXES": "/old-code", "ROUTER_CHAT_PREFIXES": "/old-chat",
            "RESEARCH_EXECUTION": "codex", "RESEARCH_PREFIXES": "/old-research",
            "RESEARCH_WEB_SEARCH": "invalid", "CODE_WORKDIR_ENV": "OLD_ROOT",
        }
        with mock.patch.dict(os.environ, {prefix + "_" + k: v for k, v in obsolete.items()}):
            effective = load_config(path, env_prefix=prefix)
        self.assertEqual(asdict(effective), asdict(baseline))

    def test_codex_command_configuration_keeps_safe_defaults(self) -> None:
        config = load_config(
            Path("/tmp/chatcopilot-missing-routing.yaml"),
            env_prefix="CHATCOPILOT_ROUTETEST",
        )

        self.assertEqual(config.routing.code_provider, "codex_cli")
        self.assertEqual(
            config.routing.code_command,
            "codex exec --model {model} --cd {workdir}",
        )

    def test_shared_binding_and_execution_environment_are_separate(self) -> None:
        import json
        from chatcopilot.core.model_settings import settings_path
        path = settings_path()
        data = json.loads(path.read_text())
        data["profiles"]["worker"].update(model="gpt-route-test", reasoning_effort="high")
        path.write_text(json.dumps(data))
        config = load_config(environment={"CHATCOPILOT_ROUTETEST_CODE_TIMEOUT_SECONDS": "17"}, env_prefix="CHATCOPILOT_ROUTETEST")
        self.assertEqual(config.routing.code_model, "gpt-route-test")
        self.assertEqual(config.routing.code_reasoning_effort, "high")
        self.assertEqual(config.routing.code_task_profile, "worker")
        self.assertEqual(config.routing.code_timeout_seconds, 17)

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

    def test_research_profile_uses_its_explicit_central_connection(self) -> None:
        main = LLMConfig(base_url="https://unused.invalid/v1", model="unused", timeout=99)
        research = load_llm_profile("research", fallback=main, environment={"CHATCOPILOT_CHAT_API_KEY": "test-key"})
        self.assertEqual(research.model, "research-default")
        self.assertEqual(research.base_url, "https://api.openai.com/v1")
        self.assertEqual(research.api_key, "test-key")


class LingyeDirectCodexConfigTests(unittest.TestCase):
    def test_lingye_uses_direct_codex_with_inprocess_search_providers(self) -> None:
        spec = load_botspec(_REPO_ROOT / "bots/lingye-copilot-qq/bot.yaml")

        self.assertEqual(spec.agents.runtime, "codex")
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
        self.assertEqual(spec.llm.code.binding, "bot.lingye-copilot-qq.code")
        self.assertNotIn("model", spec.raw["llm"]["code"])

    def test_route_explain_reports_instance_runtime_without_secrets(self) -> None:
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
                        "    binding: code",
                        "prompts:",
                        "  schema_version: 2",
                        "  identity: persona.md",
                        "  response_style: persona.md",
                        "tools:",
                        "  packs: []",
                        "agents:",
                        "  runtime: codex",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (bot_dir / "local.env").write_text(
                "export CHATCOPILOT_ROUTEDEMO_API_KEY=sk-secret\n"
                "export CHATCOPILOT_LLM_BINDING=chat\n",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = bot_cli_main(
                    ["route-explain", "--bot", str(bot_dir / "bot.yaml"), "status"]
                )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("runtime_id=codex", rendered)
        self.assertIn("selection_scope=instance", rendered)
        self.assertIn("cross_runtime_routing=false", rendered)
        self.assertIn("main.model=gpt-4o-mini", rendered)
        self.assertIn("main.reasoning_effort=", rendered)
        self.assertIn("code_task.profile=worker", rendered)
        self.assertIn("code_task.model=gpt-6-sol", rendered)
        self.assertIn("code_task.reasoning_effort=medium", rendered)
        self.assertNotIn("sk-secret", rendered)


if __name__ == "__main__":
    unittest.main()
