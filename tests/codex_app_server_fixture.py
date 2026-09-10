"""Replay shared backend scenario fixtures through the App Server event surface.

The scenarios predate the transport migration; transport tests use native RPC
frames and real pipes independently of this fixture adapter.
"""
from __future__ import annotations

import json


def app_server_replay(scenario):
    pending = iter(scenario) if isinstance(scenario, (list, tuple)) else None

    def run(command, **kwargs):
        source = next(pending) if pending is not None else scenario
        thread_id = kwargs["thread_id"] or "fixture-thread"
        started = False
        terminal = False
        sequence = 0

        def start():
            nonlocal started
            if not started:
                started = True
                kwargs["on_thread"](thread_id)
                notify("turn/started", {"turn": {"id": "fixture-turn"}})

        def notify(method, params):
            kwargs["on_notification"](method, {"threadId": thread_id, "turnId": "fixture-turn", **params})

        def consume(line):
            nonlocal sequence, thread_id, terminal
            sequence += 1
            raw = json.loads(line)
            kind = raw.get("type")
            if kind == "thread.started":
                thread_id = raw["thread_id"]
                start()
                return
            start()
            if kind in {"item.started", "item.updated", "item.completed"}:
                item = dict(raw["item"])
                item.setdefault("id", f"fixture-item-{sequence}")
                item["type"] = {"agent_message": "agentMessage", "command_execution": "commandExecution",
                    "mcp_tool_call": "mcpToolCall", "file_change": "fileChange", "web_search": "webSearch",
                    "plan_update": "plan"}.get(item["type"], item["type"])
                if "exit_code" in item:
                    item["exitCode"] = item.pop("exit_code")
                if "aggregated_output" in item:
                    item["aggregatedOutput"] = item.pop("aggregated_output")
                if item["type"] == "plan":
                    item["text"] = json.dumps(item.get("plan", []))
                notify(kind.replace(".", "/"), {"item": item})
            elif kind == "turn.completed":
                usage = raw.get("usage")
                if usage:
                    total = {target: usage.get(key, 0) for key, target in {
                        "input_tokens": "inputTokens", "output_tokens": "outputTokens",
                        "cached_input_tokens": "cachedInputTokens", "reasoning_output_tokens": "reasoningOutputTokens",
                        "cache_write_input_tokens": "cacheWriteInputTokens"}.items()}
                    total["totalTokens"] = total["inputTokens"] + total["outputTokens"]
                    notify("thread/tokenUsage/updated", {"tokenUsage": {"total": total}})
                notify("turn/completed", {"turn": {"id": "fixture-turn", "status": "completed"}})
                terminal = True

        result = source(command, **{**kwargs, "input": kwargs["prompt"], "on_stdout_line": consume}) if callable(source) else source
        if result.stdout:
            for line in result.stdout.splitlines():
                consume(line)
        start()
        if not terminal:
            notify("turn/completed", {"turn": {"id": "fixture-turn", "status": "completed" if result.returncode == 0 else "failed"}})
        return result
    return run
