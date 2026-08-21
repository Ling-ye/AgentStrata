from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from chatcopilot.botspec.model import (
    BotSpec,
    CodeLLMSpec,
    CodebaseSpec,
    ContextSpec,
    LLMSpec,
    PlatformSpec,
    PromptSpec,
    WikiSpec,
)
from chatcopilot.botspec.runtime import BotRuntimeContext
from chatcopilot.botspec.runtime_env import apply_runtime_env, load_research_llm_config
from chatcopilot.contracts.model_selection import CodeModelProfile
from chatcopilot.contracts.prompt import BotPromptProfile
from chatcopilot.core.config import LLMConfig
from chatcopilot.external_tools.codebase.config import load_registry, reset_cache


class BotSpecRuntimeEnvTests(unittest.TestCase):
    def test_research_model_uses_botspec_default_then_machine_override(self) -> None:
        fallback = LLMConfig(
            base_url="https://chat.example/v1",
            model="chat-model",
            api_key="test-key",
            timeout=60,
        )
        spec = LLMSpec(
            research_env_prefix="CHATCOPILOT_TEST_RESEARCH",
            research_model="botspec-research",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            configured = load_research_llm_config(spec, fallback=fallback)
        self.assertEqual(configured.model, "botspec-research")
        self.assertEqual(configured.base_url, fallback.base_url)
        self.assertEqual(configured.api_key, fallback.api_key)

        with mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_TEST_RESEARCH_MODEL": "machine-research"},
            clear=True,
        ):
            overridden = load_research_llm_config(spec, fallback=fallback)
        self.assertEqual(overridden.model, "machine-research")

    def test_apply_runtime_env_anchors_codebase_root_to_source_repo(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            bot_dir = root / "bots" / "demo"
            bot_dir.mkdir(parents=True)
            registry = bot_dir / "codebases" / "repositories.yaml"
            registry.parent.mkdir()
            registry.write_text("repositories: []\n", encoding="utf-8")
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(bot_yaml, registry="codebases/repositories.yaml")

            with mock.patch.dict(os.environ, {"HOME": str(root / "runtime-home")}, clear=True):
                apply_runtime_env(runtime)

                self.assertEqual(os.environ["CHATCOPILOT_SOURCE_ROOT"], str(root.resolve()))
                self.assertEqual(os.environ["CHATCOPILOT_RUNTIME_ROOT"], str(root.resolve()))
                self.assertEqual(
                    os.environ["CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT"],
                    str(root.resolve()),
                )
                self.assertEqual(os.environ["CHATCOPILOT_CODEBASE_REGISTRY"], str(registry.resolve()))

    def test_apply_runtime_env_preserves_explicit_codebase_root_override(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            bot_dir = root / "bots" / "demo"
            bot_dir.mkdir(parents=True)
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(bot_yaml)
            explicit_root = str((root / "custom-clone").resolve())

            with mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT": explicit_root},
                clear=True,
            ):
                apply_runtime_env(runtime)

                self.assertEqual(os.environ["CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT"], explicit_root)
                self.assertEqual(os.environ["CHATCOPILOT_SOURCE_ROOT"], str(root.resolve()))

    def test_apply_runtime_env_prefers_source_bot_spec_for_default_codebase_root(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source_root = base / "source"
            runtime_root = base / "runtime-copy"
            source_bot_dir = source_root / "bots" / "demo"
            runtime_bot_dir = runtime_root / "bots" / "demo"
            source_bot_dir.mkdir(parents=True)
            runtime_bot_dir.mkdir(parents=True)
            (source_root / ".git").mkdir()
            (runtime_root / "pyproject.toml").write_text("[project]\nname='copy'\n", encoding="utf-8")
            source_bot_yaml = source_bot_dir / "bot.yaml"
            runtime_bot_yaml = runtime_bot_dir / "bot.yaml"
            source_bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime_bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(runtime_bot_yaml)

            with mock.patch.dict(
                os.environ,
                {
                    "CHATCOPILOT_SOURCE_BOT_SPEC": str(source_bot_yaml),
                    "HOME": str(base / "runtime-home"),
                },
                clear=True,
            ):
                apply_runtime_env(runtime)

                self.assertEqual(os.environ["CHATCOPILOT_SOURCE_ROOT"], str(source_root.resolve()))
                self.assertEqual(os.environ["CHATCOPILOT_RUNTIME_ROOT"], str(runtime_root.resolve()))
                self.assertEqual(
                    os.environ["CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT"],
                    str(source_root.resolve()),
                )
                self.assertEqual(
                    os.environ["CHATCOPILOT_DEV_ROOT"],
                    str(source_root.resolve()),
                )

    def test_apply_runtime_env_preserves_explicit_dev_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime-copy"
            runtime_root.mkdir()
            (runtime_root / "pyproject.toml").write_text("[project]\nname='copy'\n", encoding="utf-8")
            bot_dir = runtime_root / "bots" / "demo"
            bot_dir.mkdir(parents=True)
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(bot_yaml)
            explicit_dev_root = root / "custom-dev"
            explicit_dev_root.mkdir()

            with mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_DEV_ROOT": str(explicit_dev_root)},
                clear=True,
            ):
                apply_runtime_env(runtime)

                self.assertEqual(
                    os.environ["CHATCOPILOT_DEV_ROOT"],
                    str(explicit_dev_root),
                )

    def test_apply_runtime_env_resets_registry_cache_after_env_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            bot_dir = root / "bots" / "demo"
            bot_dir.mkdir(parents=True)
            registry = bot_dir / "codebases" / "repositories.yaml"
            registry.parent.mkdir()
            registry.write_text(
                "\n".join(
                    [
                        "repositories:",
                        "  - id: chatcopilot",
                        "    display_name: AgentStrata",
                        '    root: "${CHATCOPILOT_CODEBASE_CHATCOPILOT_ROOT:-~/ChatCopilot}"',
                    ]
                ),
                encoding="utf-8",
            )
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(bot_yaml, registry="codebases/repositories.yaml")

            runtime_home = str((root / "runtime-home").resolve())
            with mock.patch.dict(
                os.environ,
                {"HOME": runtime_home, "USERPROFILE": runtime_home},
                clear=True,
            ):
                load_registry(registry, force_reload=True)
                apply_runtime_env(runtime)
                parsed = load_registry().get("chatcopilot")

                self.assertEqual(parsed.root, root.resolve())
        reset_cache()

    def test_apply_runtime_env_bridges_custom_wiki_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            bot_dir = root / "bots" / "demo"
            bot_dir.mkdir(parents=True)
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(
                bot_yaml,
                wiki=WikiSpec(enabled=True, root_env="PRIVATE_WIKI_ROOT"),
            )
            wiki_root = root / "private-wiki"

            with mock.patch.dict(
                os.environ, {"PRIVATE_WIKI_ROOT": str(wiki_root)}, clear=True
            ):
                apply_runtime_env(runtime)

                self.assertEqual(os.environ["CHATCOPILOT_WIKI_ROOT"], str(wiki_root))
                self.assertEqual(os.environ["CHATCOPILOT_WIKI_MAX_CHUNK_CHARS"], "1200")

    def test_apply_runtime_env_adds_botspec_routing_defaults_without_overriding_env(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            bot_dir = root / "bots" / "demo"
            bot_dir.mkdir(parents=True)
            bot_yaml = bot_dir / "bot.yaml"
            bot_yaml.write_text("id: demo\n", encoding="utf-8")
            runtime = _runtime(
                bot_yaml,
                llm=LLMSpec(
                    env_prefix="CHATCOPILOT_DEMO",
                    research_env_prefix="CHATCOPILOT_DEMO_RESEARCH",
                    research_execution="agent",
                    research_prefixes=("/research", "/调研"),
                    research_web_search="live",
                    code=CodeLLMSpec(
                        enabled=True,
                        model="botspec-code-model",
                        reasoning_effort="high",
                        profiles={
                            "sol-high": CodeModelProfile(
                                model="gpt-5.6-sol",
                                reasoning_effort="high",
                            )
                        },
                        code_task_profile="sol-high",
                        allowed_roles=("owner",),
                    ),
                ),
            )

            with mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_DEMO_CODE_MODEL": "env-code-model"},
                clear=True,
            ):
                apply_runtime_env(runtime)

                self.assertEqual(os.environ["CHATCOPILOT_DEMO_ROUTER_ENABLED"], "false")
                self.assertEqual(
                    os.environ["CHATCOPILOT_DEMO_RESEARCH_EXECUTION"],
                    "agent",
                )
                self.assertEqual(
                    os.environ["CHATCOPILOT_DEMO_RESEARCH_PREFIXES"],
                    "/research,/调研",
                )
                self.assertEqual(
                    os.environ["CHATCOPILOT_DEMO_RESEARCH_WEB_SEARCH"],
                    "live",
                )
                self.assertEqual(os.environ["CHATCOPILOT_DEMO_CODE_MODEL"], "env-code-model")
                self.assertEqual(
                    os.environ["CHATCOPILOT_DEMO_CODE_REASONING_EFFORT"],
                    "high",
                )
                self.assertIn(
                    '"sol-high"',
                    os.environ["CHATCOPILOT_DEMO_CODE_PROFILES_JSON"],
                )
                self.assertEqual(
                    os.environ["CHATCOPILOT_DEMO_CODE_TASK_PROFILE"],
                    "sol-high",
                )
                self.assertEqual(os.environ["CHATCOPILOT_DEMO_CODE_ALLOWED_ROLES"], "owner")


def _runtime(
    source_path: Path,
    *,
    registry: str | None = None,
    wiki: WikiSpec | None = None,
    llm: LLMSpec | None = None,
) -> BotRuntimeContext:
    spec = BotSpec(
        id="demo",
        display_name="Demo",
        source_path=source_path,
        platform=PlatformSpec(type="qq", adapter="qq_acp"),
        prompts=PromptSpec(schema_version=2, identity="persona.md", response_style="persona.md"),
        llm=llm or LLMSpec(),
        context=ContextSpec(
            codebases=CodebaseSpec(registry=registry),
            wiki=wiki or WikiSpec(),
        ),
    )
    return BotRuntimeContext(
        spec=spec,
        bot_id="demo",
        instance_id="demo",
        display_name="Demo",
        platform_type="qq",
        platform_adapter="qq_acp",
        prompt_profile=BotPromptProfile(identity="system", response_style="concise"),
        capability_policies=(),
        tool_packs=(),
        tool_features=(),
        exclude_tools=(),
        memory_namespace="demo",
        workspace_root="/tmp/workspace",
        log_dir="/tmp/logs",
        source_path=source_path,
        access=spec.access,
        subagents=spec.agents,
    )


if __name__ == "__main__":
    unittest.main()
