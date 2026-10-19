"""Opt-in contract test against the pinned client sources, without ASR/TTS model downloads."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(not os.environ.get("AGENTSTRATA_VTUBER_UPSTREAM"),
    reason="Pinned Open-LLM-VTuber source is an optional external test input")


def test_pinned_upstream_input_output_and_speech_preprocessor(monkeypatch):
    upstream = Path(os.environ["AGENTSTRATA_VTUBER_UPSTREAM"])
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(upstream / "src"))
    monkeypatch.syspath_prepend(str(root / "integrations/open_llm_vtuber"))
    from agentstrata_vtuber import runtime
    from agentstrata_vtuber.agent import AgentStrataAgent
    from agentstrata_vtuber.avatar import BrowserAvatarController
    from agentstrata_vtuber.bridge import Reply
    from agentstrata_vtuber.config import AdapterConfig
    from open_llm_vtuber.agent.agents.agent_interface import AgentInterface
    from open_llm_vtuber.agent.input_types import BatchInput, TextData, TextSource
    from open_llm_vtuber.agent.output_types import SentenceOutput
    from open_llm_vtuber.config_manager.tts_preprocessor import TTSPreprocessorConfig

    config = AdapterConfig.load(root / "integrations/open_llm_vtuber/config.example.json", {
        "AGENTSTRATA_AVATAR_GATEWAY_TOKEN": "g" * 48, "AGENTSTRATA_AVATAR_CONTROL_TOKEN": "m" * 48})
    monkeypatch.setattr(runtime, "_services", (config, BrowserAvatarController(config)))
    source = yaml.safe_load((upstream / "config_templates/conf.ZH.default.yaml").read_text())
    preprocessor = TTSPreprocessorConfig.model_validate(source["character_config"]["tts_preprocessor_config"])

    class Dialogue:
        def select_history(self, *args):
            self.history = args
        async def chat(self, text):
            return Reply("session-test", "run-test", "受控 Agent 回复。", "completed")
        async def close(self):
            pass

    async def exercise():
        agent = AgentStrataAgent(tts_preprocessor_config=preprocessor)
        assert isinstance(agent, AgentInterface)
        agent.bridge = Dialogue()
        agent.set_memory_from_history("character", "history")
        values = [output async for output in agent.chat(BatchInput([TextData(TextSource.INPUT, "你好")]))]
        assert len(values) == 1 and isinstance(values[0], SentenceOutput)
        assert values[0].display_text.text == values[0].tts_text == "受控 Agent 回复。"
        assert [v async for v in agent.chat(BatchInput([TextData(TextSource.INPUT, " ")]))] == []
        await agent.close()
    asyncio.run(exercise())
