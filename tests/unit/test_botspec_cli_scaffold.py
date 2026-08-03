from __future__ import annotations

import yaml

from chatcopilot.botspec.cli import _SYSTEM_PROMPT_TEMPLATE, _render_bot_yaml
from chatcopilot.platforms import registry


def test_bot_scaffold_uses_current_llm_slots_and_backend() -> None:
    rendered = _render_bot_yaml(
        "sample-bot",
        "feishu",
        registry.get_adapter("feishu"),
        "Sample Bot",
    )

    payload = yaml.safe_load(rendered)

    assert payload["llm"] == {"chat": {"env_prefix": "CHATCOPILOT_CHAT"}}
    assert payload["agents"]["backend"] == "native"
    assert "capabilities" not in _SYSTEM_PROMPT_TEMPLATE
    assert "prompts / tools /" in _SYSTEM_PROMPT_TEMPLATE
