"""LangGraph 会话复用的状态、上下文与工具结果辅助方法。

AgentSession 只持有会话状态；执行入口由 LangGraphAgentSession 提供。
上层通过 AgentRuntime 取得运行时会话，回合控制流由 LangGraph 负责：

1. middleware 装好 ``AgentTask``（任务文本 + 资源句柄 + 单轮 system 追加段）
2. agent 内部用 ``frame_task_message`` 把任务渲染成 user message 入栈
3. LLM ↔ 工具循环：流式 first turn，工具循环直到 LLM 不再要工具
4. 流式过程通过 ``EventSink`` 回报 ``AgentEvent``；末尾返回 ``AgentResult``

预算机制使用双层设计：
- **soft cap** → 到达后进行健康检查，健康（有进展、无停滞）则续期继续
- **hard cap** → 无条件停止（绝对安全线）
- 不健康时注入 wrap-up 指令给 LLM 一次总结机会后终止

agent 内部不感知 chat 平台、协议帧、角色等概念，所有跨层信息通过结构化协议
对象传递；payload sanitize / 权限拦截通过 hook 由 ``AgentRuntime`` 注入。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import InitVar, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.prompt_plan import render_model_prefix
from chatcopilot.agent.context.topic import TopicRelevanceClassifier
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.contracts.agent import (
    EventSink,
    TextDelta,
)
from chatcopilot.agent.response_integrity import (
    ResponseIntegrityCheck,
    ResponseIntegrityResult,
)
from chatcopilot.contracts.prompt import PromptPlan
from chatcopilot.contracts.model_runtime import RuntimeId
from chatcopilot.agent.rag.provider import Retriever, render_rag_snippet
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.agent.tools.disclosure import ToolDisclosureView
from chatcopilot.agent.turn_support import (
    safe_emit as _safe_emit,
)

_LOGGER = logging.getLogger("chatcopilot.agent.session")

ToolPayloadFilter = Callable[[Dict[str, Any]], Dict[str, Any]]
@dataclass
class AgentSession:
    """LangGraph 使用的会话状态与公共操作，不提供独立执行循环。"""

    session_id: str
    llm: LLMClient
    executor: ToolExecutor
    tools_schema: List[Dict[str, Any]]
    prompt_plan: PromptPlan
    resolved_runtime_id: InitVar[RuntimeId]
    tool_payload_filter: Optional[ToolPayloadFilter] = None
    disclosure: ToolDisclosureView | None = None
    context_manager: Optional[ContextManager] = None
    topic_classifier: Optional[TopicRelevanceClassifier] = None
    # Dual-layer iteration budget
    max_tool_iterations: int = 8          # soft cap — triggers health check
    hard_iteration_cap: int | None = None  # explicit hard budget only
    max_consecutive_tool_failures: int = 3
    max_tool_calls: Optional[int] = None
    # Dual-layer timeout budget
    timeout_seconds: Optional[int] = None       # soft timeout — check recent activity
    hard_timeout_seconds: Optional[int] = None   # absolute max time
    stall_window_seconds: int = 60               # no-progress window for soft timeout
    stream_first_turn: bool = True
    retriever: Optional[Retriever] = None
    response_integrity: ResponseIntegrityCheck = field(default_factory=ResponseIntegrityCheck)
    # 链路追踪：主会话留空（使用任务关联或生成 trace_id）；嵌套 subagent 由 runner
    # 注入父 trace_id 与父 span（让内部工具 span 挂到主调用树上）。
    trace_id: Optional[str] = None
    trace_parent_span_id: Optional[str] = None
    trace_depth: int = 0
    _runtime_id: RuntimeId = field(init=False, repr=False)
    _messages: List[Dict[str, Any]] = field(default_factory=list, init=False)
    _prompt_prefix_length: int = field(default=0, init=False)

    def __post_init__(self, resolved_runtime_id: RuntimeId) -> None:
        if resolved_runtime_id != "langgraph":
            raise ValueError("in-process AgentSession requires langgraph runtime_id")
        if self.prompt_plan.effective_runtime_id != resolved_runtime_id:
            raise ValueError("PromptPlan runtime_id does not match AgentSession runtime_id")
        self._runtime_id = resolved_runtime_id
        prefix = render_model_prefix(self.prompt_plan)
        self._messages.extend(prefix)
        self._prompt_prefix_length = len(prefix)

    # ------------------------------------------------------------------
    # 公共状态控制
    # ------------------------------------------------------------------
    @property
    def runtime_id(self) -> RuntimeId:
        """Return the immutable execution Runtime identity bound at session creation."""

        return self._runtime_id

    def set_prompt_plan(self, plan: PromptPlan) -> None:
        """Replace the immutable prompt plan while preserving conversation turns."""

        if plan.effective_runtime_id != self.runtime_id:
            raise ValueError("PromptPlan runtime_id does not match AgentSession runtime_id")
        prefix = render_model_prefix(plan)
        old_prefix_length = self._prompt_prefix_length
        self._messages[:old_prefix_length] = prefix
        self.prompt_plan = plan
        self._prompt_prefix_length = len(prefix)

    @property
    def prompt_prefix_length(self) -> int:
        """Return the host-recorded model message prefix length."""

        return self._prompt_prefix_length

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """记录没有进入 LLM 工具循环的确定性回复，让后续轮次能看到真实上下文。"""
        self._messages.append({"role": "user", "content": user_text})
        self._messages.append({"role": "assistant", "content": assistant_text})

    def snapshot_messages(self) -> List[Dict[str, Any]]:
        """返回当前 messages 的快照，供上层落 transcript。"""
        return [dict(msg) for msg in self._messages]

    def close(self) -> None:
        self.executor.close()

    def update_context(self, plan: PromptPlan) -> None:
        self.set_prompt_plan(plan)

    def snapshot_transcript(self):
        from chatcopilot.contracts.execution import TranscriptSnapshot
        from chatcopilot.core.observability_redaction import omit_private_reasoning_messages
        return TranscriptSnapshot(
            tuple(omit_private_reasoning_messages(self.snapshot_messages()).messages),
            "host_history",
        )

    def discard(self) -> None:
        self.close()

    def cancel(self) -> None:
        token = getattr(self, "_active_cancellation", None)
        if token is not None:
            token.cancel()

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def _run_response_integrity(
        self,
        final_text: str,
        *,
        successful_operations: tuple[str, ...] = (),
    ) -> ResponseIntegrityResult:
        """Run deterministic advisory integrity checks."""
        try:
            return self.response_integrity.check(
                final_text,
                successful_operations=successful_operations,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("response integrity check failed | sid=%s", self.session_id)
            return ResponseIntegrityResult(
                ok=False,
                issues=("integrity_check_failed",),
            )

    def _retrieve_context(self, query: str) -> str:
        if self.retriever is None:
            return ""
        try:
            return render_rag_snippet(self.retriever.search(query))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("RAG 检索失败 | sid=%s", self.session_id)
            return ""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_tool_call(tc: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        return name, args

    @staticmethod
    def _wrap_text_delta(on_event: EventSink) -> Callable[[str], None]:
        def _delta(text: str) -> None:
            if text:
                _safe_emit(on_event, TextDelta(text=text))

        return _delta

    def _tool_call_cap_text(self, max_tool_calls: int) -> str:
        base = (
            f"（已达单轮工具调用上限 {max_tool_calls} 次，停止本轮以避免失控。）"
        )
        evidence = self._recent_tool_result_summary()
        if not evidence:
            return base + "如有需要请把任务拆得更具体一些。"
        return (
            base
            + "\n\n已收集到的证据：\n"
            + evidence
            + "\n\n结论未完成；请基于以上证据继续追问，或提供更具体的下一步。"
        )

    def _recent_tool_result_summary(self, *, limit: int = 5, max_chars: int = 1800) -> str:
        items: list[str] = []
        for msg in reversed(self._messages):
            if msg.get("role") != "tool":
                continue
            name = str(msg.get("name") or "tool")
            raw = str(msg.get("content") or "")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"summary": raw}
            if not isinstance(payload, dict):
                payload = {"summary": str(payload)}
            ok = payload.get("ok")
            status = "ok" if ok is True else "failed" if ok is False else "unknown"
            summary = str(payload.get("summary") or payload.get("error") or "").strip()
            outputs = payload.get("outputs") if isinstance(payload.get("outputs"), list) else []
            if not summary and outputs:
                summary = "outputs: " + ", ".join(str(item) for item in outputs[:3])
            if not summary:
                continue
            summary = " ".join(summary.split())
            items.append(f"- {name} [{status}]: {summary[:360]}")
            if len(items) >= limit:
                break
        items.reverse()
        text = "\n".join(items)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"

    def _maybe_inject_budget_warning(
        self,
        llm_view: List[Dict[str, Any]],
        *,
        iteration: int,
        tool_calls_used: int,
        wrapup: bool = False,
    ) -> List[Dict[str, Any]]:
        """Inject a budget warning or wrap-up instruction into the last tool message.

        Appends to the *content* of the last ``role: tool`` message (via shallow
        copies) so the message role sequence stays ``system, user, [assistant,
        tool]*`` — this avoids injecting a non-standard ``role: system`` that
        can break DeepSeek-style prefix prompt caching.
        """
        if wrapup:
            warning = (
                "\n\n[WRAP-UP] You have reached the execution budget. "
                "You MUST produce your final answer NOW. "
                "Summarize ALL findings collected so far into a complete, "
                "user-facing response. Do NOT call any more tools."
            )
        elif self.max_tool_calls is not None:
            remaining_calls = self.max_tool_calls - tool_calls_used
            remaining_turns = (self.hard_iteration_cap - iteration - 1) if self.hard_iteration_cap is not None else None
            if remaining_calls > 2 and (remaining_turns is None or remaining_turns > 1):
                return llm_view
            warning = (
                f"\n\n[BUDGET WARNING] You have {remaining_calls} tool call(s) and "
                f"{remaining_turns if remaining_turns is not None else 'unbounded'} iteration(s) remaining. You MUST call submit_result "
                "as your very next action. Summarize all findings collected so far — "
                "do not attempt further searches."
            )
        else:
            return llm_view

        last_tool_idx = None
        for idx in range(len(llm_view) - 1, -1, -1):
            if llm_view[idx].get("role") == "tool":
                last_tool_idx = idx
                break

        if last_tool_idx is not None:
            patched = list(llm_view)
            patched_msg = dict(patched[last_tool_idx])
            patched_msg["content"] = (patched_msg.get("content") or "") + warning
            patched[last_tool_idx] = patched_msg
            return patched

        return llm_view

    # ------------------------------------------------------------------
    # dual-layer timeout
    # ------------------------------------------------------------------
    def _soft_timed_out(self, started_at: float) -> bool:
        return (
            self.timeout_seconds is not None
            and (time.monotonic() - started_at) >= self.timeout_seconds
        )

    def _hard_timed_out(self, started_at: float) -> bool:
        return (
            self.hard_timeout_seconds is not None
            and (time.monotonic() - started_at) >= self.hard_timeout_seconds
        )

    def _has_recent_tool_activity(self, last_tool_finish_time: float) -> bool:
        return (time.monotonic() - last_tool_finish_time) < self.stall_window_seconds

    # ------------------------------------------------------------------
    # stall / health detection
    # ------------------------------------------------------------------
    @staticmethod
    def _is_healthy(
        recent_fingerprints: List[str],
        consecutive_failures: int,
    ) -> bool:
        """Determine whether the agent loop is making progress.

        Unhealthy signals:
        - last 3+ tool calls are identical (stuck in a loop)
        - 2+ consecutive failures already accumulated
        """
        if consecutive_failures >= 2:
            return False
        tail = recent_fingerprints[-3:]
        if len(tail) >= 3 and len(set(tail)) == 1:
            return False
        return True

    @staticmethod
    def _repair_orphan_tool_calls(messages: List[Dict[str, Any]]) -> int:
        """Backfill synthetic error results for tool_calls missing their tool result.

        Mutates *messages* in place.  Returns the count of inserted messages.
        """
        inserted = 0
        i = 0
        while i < len(messages):
            msg = messages[i]
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                i += 1
                continue
            required: Dict[str, Dict[str, Any]] = {
                tc["id"]: tc for tc in msg["tool_calls"] if "id" in tc
            }
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                required.pop(messages[j].get("tool_call_id", ""), None)
                j += 1
            for tc_id, tc in required.items():
                name = (tc.get("function") or {}).get("name", "unknown")
                messages.insert(j, {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": name,
                    "content": json.dumps(
                        {"ok": False, "error": "aborted: turn ended before tool could execute"},
                        ensure_ascii=False,
                    ),
                })
                j += 1
                inserted += 1
            i = j
        if inserted:
            _LOGGER.warning("repaired %d orphan tool_call(s) in message history", inserted)
        return inserted


__all__ = ["AgentSession", "ToolPayloadFilter"]
