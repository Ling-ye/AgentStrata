from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec import assemble_runtime_context
from chatcopilot.botspec.loader import load_botspec, validate_botspec


def _write_bot(base: Path, agents_block: str = "") -> Path:
    bot_dir = base / "test-bot"
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "persona.md").write_text("test persona", encoding="utf-8")
    bot_yaml = bot_dir / "bot.yaml"
    lines = [
        "id: test-bot",
        "display_name: Test Bot",
        "platform:",
        "  type: feishu",
        "  adapter: feishu_acp",
        "prompts:",
        "  persona: persona.md",
        "tools:",
        "  packs: []",
    ]
    if agents_block.strip():
        lines.append("agents:")
        lines.extend(textwrap.indent(agents_block.strip(), "  ").splitlines())
    bot_yaml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bot_yaml


class AgentBackendBotSpecTests(unittest.TestCase):
    def test_default_agent_backend_is_native(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            spec = load_botspec(_write_bot(Path(tmp)))
            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

        self.assertEqual(spec.agents.backend, "native")
        self.assertEqual(spec.agents.codex.owner_access, "workspace")
        self.assertEqual(spec.agents.codex.member_access, "workspace")
        self.assertEqual(errors, [])

    def test_langgraph_backend_is_parsed_and_assembled(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            spec = load_botspec(_write_bot(Path(tmp), "backend: langgraph"))
            runtime = assemble_runtime_context(spec)

        self.assertEqual(spec.agents.backend, "langgraph")
        self.assertEqual(runtime.agent_backend, "langgraph")

    def test_codex_backend_is_parsed_and_assembled(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            spec = load_botspec(_write_bot(Path(tmp), "backend: codex"))
            runtime = assemble_runtime_context(spec)

        self.assertEqual(spec.agents.backend, "codex")
        self.assertEqual(runtime.agent_backend, "codex")

    def test_codex_main_session_policy_is_parsed_and_assembled(self) -> None:
        agents = textwrap.dedent(
            """
            backend: codex
            codex:
              owner_access: workspace
              member_access: workspace
            """
        )
        with TemporaryDirectory(dir="/tmp") as tmp:
            spec = load_botspec(_write_bot(Path(tmp), agents))
            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
            runtime = assemble_runtime_context(spec)

        self.assertEqual(errors, [])
        self.assertEqual(spec.agents.codex.owner_access, "workspace")
        self.assertEqual(spec.agents.codex.member_access, "workspace")
        self.assertEqual(runtime.subagents.codex, spec.agents.codex)

    def test_worktree_access_is_supported(self) -> None:
        agents = "backend: codex\ncodex:\n  owner_access: worktree"
        with TemporaryDirectory(dir="/tmp") as tmp:
            spec = load_botspec(_write_bot(Path(tmp), agents))
            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

        self.assertEqual(errors, [])
        self.assertEqual(spec.agents.codex.owner_access, "worktree")

    def test_invalid_codex_main_session_policy_fails_validation(self) -> None:
        cases = {
            "unsupported owner access": (
                "backend: codex\ncodex:\n  owner_access: root",
                "agents.codex.owner_access",
            ),
            "removed host access": (
                "backend: codex\ncodex:\n  owner_access: host",
                "agents.codex.owner_access",
            ),
            "removed auto publish": (
                "backend: codex\ncodex:\n  auto_publish: true",
                "agents.codex",
            ),
            "member must use workspace": (
                "backend: codex\ncodex:\n  member_access: worktree",
                "agents.codex.member_access",
            ),
            "legacy low-level policy rejected": (
                "backend: codex\ncodex:\n  sandbox: workspace-write",
                "agents.codex",
            ),
            "internal evaluation network policy rejected": (
                "backend: codex\ncodex:\n  network_access: false",
                "agents.codex",
            ),
            "internal evaluation web policy rejected": (
                "backend: codex\ncodex:\n  web_search_mode: disabled",
                "agents.codex",
            ),
            "internal evaluation sandbox policy rejected": (
                "backend: codex\ncodex:\n  sandbox_mode: read-only",
                "agents.codex",
            ),
            "internal evaluation delegate policy rejected": (
                "backend: codex\ncodex:\n  allow_delegate_tools: true",
                "agents.codex",
            ),
            "internal evaluation unified search policy rejected": (
                "backend: codex\ncodex:\n  allow_unified_search_tool: true",
                "agents.codex",
            ),
            "cross backend policy rejected": (
                "backend: native\ncodex:\n  owner_access: worktree",
                "agents.codex",
            ),
        }
        for label, (agents, field) in cases.items():
            with self.subTest(label=label), TemporaryDirectory(dir="/tmp") as tmp:
                spec = load_botspec(_write_bot(Path(tmp), agents))
                errors = [
                    issue
                    for issue in validate_botspec(spec)
                    if issue.level == "error" and issue.field == field
                ]
            self.assertTrue(errors, label)

    def test_unknown_agent_backend_is_validation_error(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            spec = load_botspec(_write_bot(Path(tmp), "backend: unknown"))
            errors = [
                issue
                for issue in validate_botspec(spec)
                if issue.level == "error" and issue.field == "agents.backend"
            ]

        self.assertEqual(len(errors), 1)
        self.assertIn("native, langgraph, codex", errors[0].message)

    def test_removed_default_route_is_an_immediate_validation_error(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            path = _write_bot(Path(tmp), "backend: native")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    "llm:\n  code:\n    default_route: code\n"
                )
            spec = load_botspec(path)
            errors = [
                issue
                for issue in validate_botspec(spec)
                if issue.field == "llm.code.default_route"
            ]

        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
