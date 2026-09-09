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
        "  schema_version: 2",
        "  identity: persona.md",
        "  response_style: persona.md",
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

    def test_retired_codex_permission_fields_explain_migration(self) -> None:
        fields = {
            "owner_access": "worktree",
            "member_access": "workspace",
            "sandbox_mode": "read-only",
            "allow_delegate_tools": "true",
        }
        for name, value in fields.items():
            with self.subTest(field=name), TemporaryDirectory(dir="/tmp") as tmp:
                with self.assertRaisesRegex(
                    ValueError, "retired permission fields; remove this block"
                ):
                    load_botspec(
                        _write_bot(Path(tmp), f"backend: codex\ncodex:\n  {name}: {value}")
                    )

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

    def test_removed_agents_persona_control_points_to_tool_pack(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            path = _write_bot(
                Path(tmp),
                "persona_control:\n  enabled: true",
            )
            with self.assertRaisesRegex(
                ValueError,
                r"agents\.persona_control was removed; enable persona\.control",
            ):
                load_botspec(path)

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
