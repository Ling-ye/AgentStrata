"""上下文窗口管理器。

ContextManager 是纯函数式的视图生成器：接收完整 _messages，返回裁剪后的
副本供 LLM 调用，**绝不修改**原始列表。

裁剪流水线（prepare_messages 内部顺序）：
1. 分离 system prompt（永远保留，不做任何修改以最大化 prefix cache）
2. _segment_turns 按 user message 边界分割轮次
3. 延迟摘要化：窗口外的 tool 消息截断为 summary
4. 可选：summarize_prior_tool_results 对窗口内前轮工具结果摘要化
5. 滑动窗口：保留最近 N 轮
6. token 上限裁剪：对话部分超限时整轮丢弃最老轮次
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

from chatcopilot.agent.context.token_estimator import (
    estimate_message_tokens,
    estimate_messages_tokens,
)

_LOGGER = logging.getLogger("chatcopilot.agent.context.manager")


@dataclass
class Turn:
    """A conversation turn: one user message followed by assistant/tool messages."""

    messages: List[Dict[str, Any]]
    start_idx: int
    end_idx: int  # exclusive


def _segment_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    """Split non-system messages into turns by user-message boundaries.

    Each turn starts with a ``role: user`` message and includes all
    subsequent ``assistant`` / ``tool`` messages until the next ``user``
    or end of list.
    """
    turns: List[Turn] = []
    current_msgs: List[Dict[str, Any]] = []
    current_start = -1

    for idx, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "system":
            continue
        if role == "user":
            if current_msgs:
                turns.append(Turn(messages=current_msgs, start_idx=current_start, end_idx=idx))
            current_msgs = [msg]
            current_start = idx
        else:
            if not current_msgs:
                current_msgs = [msg]
                current_start = idx
            else:
                current_msgs.append(msg)

    if current_msgs:
        turns.append(Turn(
            messages=current_msgs,
            start_idx=current_start,
            end_idx=current_start + len(current_msgs),
        ))
    return turns


def _summarize_tool_message(msg: Dict[str, Any], max_tokens: int) -> Dict[str, Any]:
    """Return a summarized copy of a tool message if it exceeds the threshold."""
    content_raw = msg.get("content") or ""
    if not isinstance(content_raw, str):
        content_raw = json.dumps(content_raw, ensure_ascii=False)

    from chatcopilot.agent.context.token_estimator import estimate_tokens
    if estimate_tokens(content_raw) <= max_tokens:
        return msg

    try:
        payload = json.loads(content_raw)
    except (json.JSONDecodeError, TypeError):
        payload = {}

    summary = ""
    if isinstance(payload, dict):
        summary = payload.get("summary") or payload.get("error") or ""
    if not summary:
        summary = content_raw[:200] + "..."

    summarized = dict(msg)
    summarized["content"] = json.dumps(
        {"summary": summary, "truncated": True},
        ensure_ascii=False,
    )
    return summarized


@dataclass(frozen=True)
class ContextManager:
    """Pure-functional context window manager.

    Call ``prepare_messages`` before each LLM invocation to get a trimmed
    copy.  The original messages list is never modified.
    """

    max_context_tokens: int = 16000
    sliding_window_turns: int = 3
    tool_result_summary_max_tokens: int = 500
    summarize_prior_tool_results: bool = False

    def prepare_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        topic_decision: Any = None,
    ) -> List[Dict[str, Any]]:
        """Return a trimmed deep-copy of *messages* for LLM consumption."""
        if not messages:
            return []

        working = copy.deepcopy(messages)

        system_msg, conversation = self._split_system(working)
        turns = _segment_turns(conversation)

        if not turns:
            unsegmented = [system_msg] if system_msg else []
            unsegmented.extend(conversation)
            return unsegmented

        topic_context = getattr(topic_decision, "context_kind", None)
        if topic_context == "unrelated":
            turns = turns[-1:]
            unrelated_result: List[Dict[str, Any]] = []
            if system_msg:
                unrelated_result.append(system_msg)
            unrelated_result.extend(turns[0].messages)
            _LOGGER.debug(
                "context window | topic=unrelated turns_kept=1 turns_trimmed=%d",
                max(0, len(_segment_turns(conversation)) - 1),
            )
            return unrelated_result

        window_boundary = max(0, len(turns) - self.sliding_window_turns)
        for turn in turns[:window_boundary]:
            for i, msg in enumerate(turn.messages):
                if msg.get("role") == "tool":
                    turn.messages[i] = _summarize_tool_message(
                        msg, self.tool_result_summary_max_tokens
                    )

        if self.summarize_prior_tool_results:
            for turn in turns:
                _summarize_prior_iteration_tools(
                    turn.messages, self.tool_result_summary_max_tokens
                )

        conv_tokens = sum(
            estimate_messages_tokens(t.messages) for t in turns
        )
        trimmed_count = 0
        while conv_tokens > self.max_context_tokens and turns and len(turns) > 1:
            oldest = turns.pop(0)
            conv_tokens -= estimate_messages_tokens(oldest.messages)
            trimmed_count += 1

        prepared: List[Dict[str, Any]] = []
        if system_msg:
            prepared.append(system_msg)

        for turn in turns:
            prepared.extend(turn.messages)

        if _LOGGER.isEnabledFor(logging.DEBUG):
            system_tokens = estimate_message_tokens(system_msg) if system_msg else 0
            _LOGGER.debug(
                "context window | system ~%d tokens | conversation ~%d/%d tokens | "
                "%d turns kept | %d trimmed%s",
                system_tokens, conv_tokens, self.max_context_tokens,
                len(turns), trimmed_count,
                f" | topic={topic_context}" if topic_context else "",
            )

        return prepared

    @staticmethod
    def _split_system(
        messages: List[Dict[str, Any]],
    ) -> tuple[Dict[str, Any] | None, List[Dict[str, Any]]]:
        """Separate the leading system message from conversation messages."""
        if messages and messages[0].get("role") == "system":
            return messages[0], messages[1:]
        return None, messages


def _summarize_prior_iteration_tools(
    messages: List[Dict[str, Any]], max_tokens: int
) -> None:
    """Summarize tool results from earlier iterations within a single turn.

    In subagent sessions the conversation often has only one turn (a single
    user message followed by multiple assistant→tool cycles). The standard
    sliding-window logic never summarizes these because they are all "in-window".

    This function finds the last assistant message that triggered tool calls and
    summarizes all tool results *before* that iteration boundary so the LLM
    still sees the latest tool outputs in full while prior iterations are
    compacted.
    """
    last_assistant_tc_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_assistant_tc_idx = i
            break

    if last_assistant_tc_idx < 0:
        return

    for i in range(last_assistant_tc_idx):
        if messages[i].get("role") == "tool":
            messages[i] = _summarize_tool_message(messages[i], max_tokens)


__all__ = ["ContextManager", "Turn", "_segment_turns"]
