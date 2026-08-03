from __future__ import annotations

from pathlib import Path


def test_top_level_cli_uses_lazy_imports_for_heavy_commands() -> None:
    text = Path("src/chatcopilot/__main__.py").read_text(encoding="utf-8")
    pre_main = text.split("def main", 1)[0]

    assert "chatcopilot.run" not in pre_main
    assert "chatcopilot.middleware." not in pre_main
    assert "chatcopilot.platforms.qq.at_proxy" not in pre_main
    assert "from chatcopilot.botspec.cli import main as bot_main" in text
    assert "from chatcopilot.evals.cli import main as evals_main" in text
    assert "agentstrata evals list" in text
    assert "agentstrata http-api-server" in text
    assert "agentstrata qq-at-proxy" in text
    assert "Compatibility entry point: python -m chatcopilot <command>" in text
