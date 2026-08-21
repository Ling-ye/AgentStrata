from __future__ import annotations

import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.rag import load_rag_source_configs


def _write_bot(base: Path, *, rag_sources: str | None = None) -> Path:
    bot_dir = base / "test-bot"
    (bot_dir / "prompts").mkdir(parents=True)
    (bot_dir / "prompts" / "persona.md").write_text("test bot\n", encoding="utf-8")
    if rag_sources is not None:
        (bot_dir / "rag").mkdir()
        (bot_dir / "rag" / "sources.yaml").write_text(rag_sources, encoding="utf-8")
    lines = [
        "id: test-bot",
        "display_name: Test Bot",
        "platform:",
        "  type: feishu",
        "  adapter: feishu_acp",
        "prompts:",
        "  schema_version: 2",
        "  identity: prompts/persona.md",
        "  response_style: prompts/persona.md",
        "tools:",
        "  packs:",
        "    - workspace.read_write",
    ]
    if rag_sources is not None:
        lines.extend(["context:", "  rag:", "    sources: rag/sources.yaml"])
    lines.extend(["deploy:", "  target: wsl2"])
    (bot_dir / "bot.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bot_dir / "bot.yaml"


class RagConfigTests(unittest.TestCase):
    def test_missing_rag_sources_file_is_optional(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot(Path(tmp)))

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

            self.assertEqual(errors, [])
            self.assertEqual(load_rag_source_configs(spec), ())

    def test_loads_relative_source_config(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_yaml = _write_bot(
                Path(tmp),
                rag_sources=textwrap.dedent(
                    """\
                    sources:
                      - path: docs
                        include: ["*.md"]
                        exclude: ["draft.md"]
                        max_chunk_chars: 800
                    """
                ),
            )
            spec = load_botspec(bot_yaml)

            configs = load_rag_source_configs(spec)

            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].path, (bot_yaml.parent / "docs").resolve())
            self.assertEqual(configs[0].include, ("*.md",))
            self.assertEqual(configs[0].exclude, ("draft.md",))
            self.assertEqual(configs[0].max_chunk_chars, 800)

    def test_rejects_plain_absolute_source_path(self) -> None:
        with TemporaryDirectory() as tmp:
            bot_yaml = _write_bot(
                Path(tmp),
                rag_sources=f"sources:\n  - path: {Path(tmp).as_posix()}\n",
            )
            spec = load_botspec(bot_yaml)

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

            self.assertTrue(any("绝对路径" in issue.message for issue in errors))

    def test_rejects_relative_source_escape(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot(Path(tmp), rag_sources="sources:\n  - path: ../outside\n"))

            errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]

            self.assertTrue(any("逃逸" in issue.message for issue in errors))

    def test_env_source_path_is_allowed_when_set(self) -> None:
        with TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "shared"
            source_dir.mkdir()
            old = os.environ.get("TEST_RAG_ROOT")
            os.environ["TEST_RAG_ROOT"] = str(source_dir)
            try:
                spec = load_botspec(_write_bot(Path(tmp), rag_sources="sources:\n  - path: ${TEST_RAG_ROOT}\n"))
                errors = [issue for issue in validate_botspec(spec) if issue.level == "error"]
                configs = load_rag_source_configs(spec)
            finally:
                if old is None:
                    os.environ.pop("TEST_RAG_ROOT", None)
                else:
                    os.environ["TEST_RAG_ROOT"] = old

            self.assertEqual(errors, [])
            self.assertEqual(configs[0].path, source_dir.resolve())


if __name__ == "__main__":
    unittest.main()
