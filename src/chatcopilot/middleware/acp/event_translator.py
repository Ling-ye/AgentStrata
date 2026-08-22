"""AgentEvent → ACP session_update 翻译器。

``AgentSession.run_task`` 在子线程跑同步循环，事件回调（``on_event``）也在子线程
触发；ACP server 的 ``session_update`` 协程必须在主 event loop 投递。本模块把
两者桥接起来，并实现 debug_mode 过滤、可见进度兜底、流式文本静默缓存等飞书桥
特有的策略，让 server.py 不再持有 90 行的事件闭包。

行为契约（与重构前完全等价）：
- 非 debug 模式：``TextDelta`` 与 ``FinalText`` 仅缓存最后一段；首次 ``ToolStarted``
  推一条可见的进度（用 cached preview 或 "收到，正在处理"）；``ToolFinished`` 吞掉。
- debug 模式：``TextDelta`` 仍仅缓存；``ToolStarted`` 推 "⚙️ 正在调用工具 X"；
  ``ToolFinished`` 推 "✅/❌ X 完成 + summary"。
- 本轮末尾由 server.py 调 ``flush_final()`` 等待挂起 future 后推 last_text。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from chatcopilot.contracts.agent import (
    AgentEvent,
    FinalText,
    SpanFinished,
    SpanStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnError,
)

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.event_translator")


class EventTranslator:
    """把一轮 ``run_task`` 的 AgentEvent 流翻译成 ACP session_update。"""

    def __init__(
        self,
        *,
        conn: Any,
        session_id: str,
        debug_mode: bool,
        loop: asyncio.AbstractEventLoop,
        update_agent_message_text: Any,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._debug_mode = debug_mode
        self._loop = loop
        self._update_agent_message_text = update_agent_message_text
        self._pending_pushes: List[Any] = []
        # 用 dict 而不是 nonlocal，因为闭包要在 asyncio.to_thread 子线程里写。
        self._last_text_cache: Dict[str, str] = {"text": ""}
        self._stream_text_cache: Dict[str, str] = {"text": ""}
        self._visible_progress_sent: Dict[str, bool] = {"sent": False}

    # ------------------------------------------------------------------
    # 子线程入口：on_event 回调
    # ------------------------------------------------------------------
    def dispatch(self, event: AgentEvent) -> None:
        if isinstance(event, TextDelta):
            self._on_text_delta(event.text)
        elif isinstance(event, FinalText):
            self._on_final_text(event.text)
        elif isinstance(event, ToolStarted):
            self._on_tool_start(event.name, dict(event.arguments))
        elif isinstance(event, ToolFinished):
            summary = event.summary if event.ok else (event.error or "工具执行失败")
            self._on_tool_end(event.name, event.ok, summary)
        elif isinstance(event, SpanStarted):
            # subagent 委托边界：非 debug 复用工具进度兜底；debug 显式提示。
            if self._debug_mode:
                self._push(f"🧩 委托 `{event.name}` …")
            else:
                self._on_tool_start(event.name, {})
        elif isinstance(event, SpanFinished):
            if self._debug_mode:
                marker = "✅" if event.ok else "❌"
                self._push(f"{marker} 委托 `{event.name}` 完成。")
        elif isinstance(event, TurnError):
            _LOGGER.warning(
                "agent turn error | sid=%s code=%s message=%s",
                self._session_id,
                event.code,
                event.message,
            )

    # ------------------------------------------------------------------
    # 主 loop 入口：推送 + flush
    # ------------------------------------------------------------------
    @property
    def last_text(self) -> str:
        return self._last_text_cache["text"]

    def reset_text_cache(self) -> None:
        """Discard one unflushed attempt before a bounded runtime retry."""

        self._last_text_cache["text"] = ""
        self._stream_text_cache["text"] = ""

    def replace_final_text(self, text: str) -> None:
        """Install a deterministic runtime-verified final response."""

        self._stream_text_cache["text"] = ""
        self._last_text_cache["text"] = text

    async def await_pending(self) -> None:
        """等待之前调度的 session_update 全部完成；debug 模式下避免最终文本被进度消息插队覆盖。"""
        if not self._pending_pushes:
            return
        await asyncio.gather(
            *(asyncio.wrap_future(future) for future in self._pending_pushes),
            return_exceptions=True,
        )

    async def flush_final(self, fallback_text: str = "") -> str:
        """本轮末尾统一推 last_text 或 fallback；返回实际推送的文本。"""
        await self.await_pending()
        text = self._last_text_cache["text"] or fallback_text
        if text:
            await self._conn.session_update(
                session_id=self._session_id,
                update=self._update_agent_message_text(text),
            )
        return text

    # ------------------------------------------------------------------
    # 内部 dispatch helpers
    # ------------------------------------------------------------------
    def _push(self, text: str) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._conn.session_update(
                    session_id=self._session_id,
                    update=self._update_agent_message_text(text),
                ),
                self._loop,
            )
            self._pending_pushes.append(future)
        except RuntimeError:
            _LOGGER.exception("session_update scheduling failed")

    def _on_text_delta(self, delta: str) -> None:
        if not delta:
            return
        self._stream_text_cache["text"] = self._stream_text_cache["text"] + delta
        self._last_text_cache["text"] = self._stream_text_cache["text"]

    def _on_final_text(self, text: str) -> None:
        if not text:
            return
        self._last_text_cache["text"] = text  # 静默缓存，本轮末尾再推

    def _on_tool_start(self, name: str, args: Dict[str, Any]) -> None:
        if not self._debug_mode:
            if self._visible_progress_sent["sent"]:
                return
            self._visible_progress_sent["sent"] = True
            preview = (self._last_text_cache["text"] or "").strip()
            if preview:
                if len(preview) > 240:
                    preview = preview[:240] + "…"
                self._push(preview)
            else:
                self._push("收到，正在处理你的请求，请稍等。")
            return
        self._push(f"⚙️ 正在调用工具 `{name}` …")

    def _on_tool_end(self, name: str, ok: bool, summary: str) -> None:
        if not self._debug_mode:
            return  # 非 debug：吞掉"工具完成"提示
        marker = "✅" if ok else "❌"
        summary_clipped = (summary or "").strip()
        if len(summary_clipped) > 400:
            summary_clipped = summary_clipped[:400] + "…"
        text = (
            f"{marker} `{name}` 完成。\n{summary_clipped}"
            if summary_clipped
            else f"{marker} `{name}` 完成。"
        )
        self._push(text)


__all__ = ["EventTranslator"]
