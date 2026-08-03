"""ContextManager + token_estimator unit tests."""
from __future__ import annotations

import json


from chatcopilot.agent.context.manager import ContextManager, _segment_turns
from chatcopilot.agent.context.topic import TopicDecision
from chatcopilot.agent.context.token_estimator import (
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
)


# ---------------------------------------------------------------------------
# token_estimator
# ---------------------------------------------------------------------------
class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_ascii(self):
        result = estimate_tokens("hello world")
        assert result >= 1

    def test_cjk(self):
        result = estimate_tokens("你好世界")
        assert result >= 4

    def test_mixed(self):
        text = "hello 你好"
        result = estimate_tokens(text)
        assert result > estimate_tokens("hello")

    def test_message_overhead(self):
        msg = {"role": "user", "content": "hi"}
        tokens = estimate_message_tokens(msg)
        assert tokens >= 5  # 4 overhead + at least 1 for content

    def test_messages_total(self):
        msgs = [
            {"role": "system", "content": "you are a bot"},
            {"role": "user", "content": "hi"},
        ]
        total = estimate_messages_tokens(msgs)
        assert total == sum(estimate_message_tokens(m) for m in msgs)


# ---------------------------------------------------------------------------
# _segment_turns
# ---------------------------------------------------------------------------
class TestSegmentTurns:
    def test_empty(self):
        assert _segment_turns([]) == []

    def test_system_only(self):
        msgs = [{"role": "system", "content": "sys"}]
        assert _segment_turns(msgs) == []

    def test_single_user(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
        ]
        turns = _segment_turns(msgs)
        assert len(turns) == 1
        assert turns[0].messages == [{"role": "user", "content": "q1"}]

    def test_user_assistant_pair(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        turns = _segment_turns(msgs)
        assert len(turns) == 1
        assert len(turns[0].messages) == 2

    def test_multiple_turns(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        turns = _segment_turns(msgs)
        assert len(turns) == 2
        assert turns[0].messages[0]["content"] == "q1"
        assert turns[1].messages[0]["content"] == "q2"

    def test_tool_messages_grouped_with_assistant(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "tool_call_id": "tc1", "name": "foo", "content": "{}"},
            {"role": "assistant", "content": "done"},
        ]
        turns = _segment_turns(msgs)
        assert len(turns) == 1
        assert len(turns[0].messages) == 4

    def test_record_exchange_turns(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "upload"},
            {"role": "assistant", "content": "file saved"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        turns = _segment_turns(msgs)
        assert len(turns) == 2


# ---------------------------------------------------------------------------
# ContextManager.prepare_messages
# ---------------------------------------------------------------------------
def _make_system(content: str = "system prompt") -> dict:
    return {"role": "system", "content": content}


def _make_turn(user: str, assistant: str) -> list[dict]:
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]


def _make_tool_turn(user: str, tool_name: str, tool_content: str, assistant: str) -> list[dict]:
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"tc_{tool_name}", "type": "function",
             "function": {"name": tool_name, "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": f"tc_{tool_name}", "name": tool_name,
         "content": json.dumps({"ok": True, "summary": "short", "outputs": [], "console_tail": tool_content})},
        {"role": "assistant", "content": assistant},
    ]


class TestContextManagerPrepare:
    def test_empty_messages(self):
        cm = ContextManager()
        assert cm.prepare_messages([]) == []

    def test_preserves_system(self):
        msgs = [_make_system("hello system")]
        result = cm_result(msgs)
        assert result[0]["role"] == "system"
        assert "hello system" in result[0]["content"]

    def test_no_trimming_within_budget(self):
        msgs = [_make_system(), *_make_turn("q1", "a1")]
        cm = ContextManager(max_context_tokens=50000, sliding_window_turns=10)
        result = cm.prepare_messages(msgs)
        assert len(result) == 3  # system + user + assistant

    def test_sliding_window_keeps_recent(self):
        msgs = [_make_system()]
        for i in range(5):
            msgs.extend(_make_turn(f"q{i}", f"a{i}"))
        cm = ContextManager(max_context_tokens=50000, sliding_window_turns=2)
        result = cm.prepare_messages(msgs)
        user_contents = [m["content"] for m in result if m.get("role") == "user"]
        assert "q3" in user_contents
        assert "q4" in user_contents

    def test_token_limit_trims_old_turns(self):
        msgs = [_make_system("sys")]
        for i in range(10):
            msgs.extend(_make_turn(f"question_{i}_" + "x" * 500, f"answer_{i}_" + "y" * 500))
        cm = ContextManager(max_context_tokens=1000, sliding_window_turns=2)
        result = cm.prepare_messages(msgs)
        assert len(result) < len(msgs)
        assert result[0]["role"] == "system"

    def test_tool_result_summarized_outside_window(self):
        big_content = "A" * 5000
        msgs = [
            _make_system(),
            *_make_tool_turn("q0", "big_tool", big_content, "done0"),
            *_make_turn("q1", "a1"),
            *_make_turn("q2", "a2"),
            *_make_turn("q3", "a3"),
        ]
        cm = ContextManager(
            max_context_tokens=50000,
            sliding_window_turns=2,
            tool_result_summary_max_tokens=50,
        )
        result = cm.prepare_messages(msgs)
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        if tool_msgs:
            payload = json.loads(tool_msgs[0]["content"])
            assert payload.get("truncated") is True

    def test_metadata_injected(self):
        """System content is NOT modified (metadata goes to log only for cache friendliness)."""
        msgs = [_make_system("base"), *_make_turn("q1", "a1")]
        cm = ContextManager()
        result = cm.prepare_messages(msgs)
        assert result[0]["content"] == "base"

    def test_does_not_mutate_original(self):
        msgs = [_make_system("original system")]
        for i in range(5):
            msgs.extend(_make_turn(f"q{i}", f"a{i}"))
        original_len = len(msgs)
        original_system = msgs[0]["content"]
        cm = ContextManager(max_context_tokens=100, sliding_window_turns=1)
        cm.prepare_messages(msgs)
        assert len(msgs) == original_len
        assert msgs[0]["content"] == original_system

    def test_never_drops_last_turn(self):
        msgs = [_make_system("sys")]
        msgs.extend(_make_turn("only question", "only answer"))
        cm = ContextManager(max_context_tokens=10, sliding_window_turns=1)
        result = cm.prepare_messages(msgs)
        user_msgs = [m for m in result if m.get("role") == "user"]
        assert len(user_msgs) >= 1

    def test_metadata_shows_trimmed_count(self):
        """Trimming still works; system content stays unchanged."""
        msgs = [_make_system("sys")]
        for i in range(6):
            msgs.extend(_make_turn(f"q{i}_" + "x" * 200, f"a{i}_" + "y" * 200))
        cm = ContextManager(max_context_tokens=500, sliding_window_turns=2)
        result = cm.prepare_messages(msgs)
        system_content = result[0]["content"]
        assert system_content == "sys"
        user_msgs = [m for m in result if m.get("role") == "user"]
        assert len(user_msgs) < 6

    def test_unrelated_topic_keeps_only_current_turn_view(self):
        msgs = [_make_system("sys")]
        msgs.extend(_make_turn("分析 csv", "分析完成"))
        msgs.append({"role": "user", "content": "北京明天天气怎么样？"})
        cm = ContextManager(max_context_tokens=50000, sliding_window_turns=10)
        decision = TopicDecision.unrelated(source="llm", reason="standalone")

        result = cm.prepare_messages(msgs, topic_decision=decision)

        user_contents = [m["content"] for m in result if m.get("role") == "user"]
        assert user_contents == ["北京明天天气怎么样？"]
        assert result[0]["content"] == "sys"
        assert len(msgs) == 4


def cm_result(msgs, **kwargs):
    cm = ContextManager(**kwargs)
    return cm.prepare_messages(msgs)
