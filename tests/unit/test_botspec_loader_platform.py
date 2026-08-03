"""BotSpec loader 的 platform.type 白名单校验单测。"""
from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from chatcopilot.botspec.loader import load_botspec, validate_botspec


def _write_bot(
    base: Path,
    *,
    platform_type: str,
    platform_adapter: str = "test_adapter",
) -> Path:
    """生成一个最小可加载的 bot.yaml + system prompt 文件。"""
    bot_dir = base / "test-bot"
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "system.md").write_text("# 测试机器人\n", encoding="utf-8")
    yaml_text = textwrap.dedent(
        f"""\
        id: test-bot
        display_name: 测试机器人
        platform:
          type: {platform_type}
          adapter: {platform_adapter}
        prompts:
          persona: system.md
        tools:
          packs: []
        deploy:
          target: wsl2
        """
    )
    bot_yaml = bot_dir / "bot.yaml"
    bot_yaml.write_text(yaml_text, encoding="utf-8")
    return bot_yaml


class BotSpecPlatformWhitelistTests(unittest.TestCase):
    def test_feishu_passes_platform_check(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot(Path(tmp), platform_type="feishu"))
            errors = [
                issue
                for issue in validate_botspec(spec)
                if issue.level == "error" and issue.field == "platform.type"
            ]
            self.assertEqual(errors, [], msg="feishu 应当通过 platform.type 白名单")

    def test_qq_passes_platform_check(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot(Path(tmp), platform_type="qq"))
            errors = [
                issue
                for issue in validate_botspec(spec)
                if issue.level == "error" and issue.field == "platform.type"
            ]
            self.assertEqual(errors, [], msg="qq 应当通过 platform.type 白名单")

    def test_unknown_platform_reports_error(self) -> None:
        with TemporaryDirectory() as tmp:
            spec = load_botspec(_write_bot(Path(tmp), platform_type="twitter"))
            errors = [
                issue
                for issue in validate_botspec(spec)
                if issue.level == "error" and issue.field == "platform.type"
            ]
            self.assertEqual(len(errors), 1)
            self.assertIn("platform.type 仅支持", errors[0].message)


if __name__ == "__main__":
    unittest.main()
