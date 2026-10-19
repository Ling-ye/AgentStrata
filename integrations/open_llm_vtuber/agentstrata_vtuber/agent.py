"""The only module importing upstream Agent types; loaded only in its environment."""
from __future__ import annotations

import asyncio
import json
import logging

from open_llm_vtuber.agent.agents.agent_interface import AgentInterface
from open_llm_vtuber.agent.input_types import BatchInput, TextSource
from open_llm_vtuber.agent.output_types import Actions, DisplayText, SentenceOutput
from open_llm_vtuber.config_manager.tts_preprocessor import TTSPreprocessorConfig
from open_llm_vtuber.utils.tts_preprocessor import tts_filter

from chatcopilot.protocols.gateway_client import GatewayClientError, GatewayWebSocketClient
from .bridge import ConversationBridge
from .runtime import services

_LOG = logging.getLogger(__name__)


class AgentStrataAgent(AgentInterface):
    def __init__(self, *, tts_preprocessor_config: TTSPreprocessorConfig, send_text=None):
        config, _ = services()
        self.bridge = ConversationBridge(GatewayWebSocketClient(config.gateway), client_id=config.gateway.client_id)
        self.tts_config = tts_preprocessor_config
        self._send_text = send_text
        self._stop_tasks: set[asyncio.Task] = set()

    async def chat(self, input_data):
        if not isinstance(input_data, BatchInput) or input_data.images or input_data.files:
            yield SentenceOutput(DisplayText("第一版仅支持文字和按键语音输入。"), "", Actions())
            return
        text = "\n".join(item.content for item in input_data.texts if item.source is TextSource.INPUT).strip()
        if not text:
            return
        try:
            if text == "/resume":
                reply = await self.bridge.resume()
            elif text == "/stop":
                await self.bridge.cancel()
                yield SentenceOutput(DisplayText("当前停止操作已处理。"), "", Actions())
                return
            else:
                reply = await self.bridge.chat(text)
            if reply.state != "completed":
                return
            if self._send_text is not None:
                await self._send_text(json.dumps({"type": "agentstrata-reply",
                    "session_id": reply.session_id, "run_id": reply.run_id}))
            config = self.tts_config
            speech = tts_filter(reply.text,
                remove_special_char=config.remove_special_char,
                ignore_brackets=config.ignore_brackets,
                ignore_parentheses=config.ignore_parentheses,
                ignore_asterisks=config.ignore_asterisks,
                ignore_angle_brackets=config.ignore_angle_brackets)
            yield SentenceOutput(DisplayText(reply.text), speech, Actions())
        except (GatewayClientError, RuntimeError, ValueError) as exc:
            # Backend error codes are safe; never print credentials or raw transport frames.
            code = getattr(exc, "code", type(exc).__name__)
            _LOG.warning("avatar dialogue unavailable | code=%s", code)
            yield SentenceOutput(DisplayText(
                "AgentStrata 暂未完成本轮请求。请检查后端连接；使用 /resume 查询原请求，或停止后再发送。"), "", Actions())

    def handle_interrupt(self, heard_response: str) -> None:
        # Heard text is presentation evidence, never a replacement for the native transcript.
        del heard_response
        task = asyncio.get_running_loop().create_task(self._stop())
        self._stop_tasks.add(task)
        task.add_done_callback(self._stop_tasks.discard)

    async def _stop(self):
        try:
            await self.bridge.cancel()
        except (GatewayClientError, RuntimeError):
            _LOG.warning("avatar stop outcome unknown; original run was not replayed")

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        self.bridge.select_history(conf_uid, history_uid)

    async def close(self) -> None:
        if self._stop_tasks:
            await asyncio.gather(*self._stop_tasks)
        await self.bridge.close()
