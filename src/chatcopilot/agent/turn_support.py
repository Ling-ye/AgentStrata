"""Shared turn constants and pure helpers, independent of session implementations."""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal, cast

from chatcopilot.agent.protocol import AgentEvent, AgentTask, EventSink, ResourceRef
from chatcopilot.agent.tools.executor import ToolResult

LOGGER = logging.getLogger("chatcopilot.agent.turn_support")

EMPTY_MODEL_REPLY_TEXT = "（模型这次没有返回有效内容，请再发送一次或补充更多上下文。）"
SEARCH_INFORMATION_TOOL = "search_information"
REPEATED_SEARCH_SUMMARY_LIMIT = 24000
DEV_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "delete_file",
    "approve_mcp_server",
}
FINALIZE_SELF_UPDATE_TOOL = "finalize_self_update"
SELF_UPDATE_FINAL_TOOL_NAMES = {"submit_result"}
SELF_UPDATE_REQUIRED_PROMPT = (
    "[SELF-UPDATE REQUIRED] You modified repository files with "
    "a source configuration or repository file. You MUST call finalize_self_update next "
    "with a concise reason after drafting a non-empty user-facing final summary. "
    "You may include that summary in the same assistant message as the tool call. "
    "Do not call submit_result until finalize_self_update is accepted."
)


def task_trace_id(task: AgentTask) -> str | None:
    value = task.metadata.get("trace_id") if task.metadata else None
    text = str(value).strip() if value else ""
    return text or None


def safe_emit(on_event: EventSink, event: AgentEvent) -> None:
    try:
        on_event(event)
    except Exception:  # noqa: BLE001
        LOGGER.exception("agent event sink raised, ignored | event=%s", type(event).__name__)


def tool_fingerprint(name: str, args: dict[str, Any]) -> str:
    raw = json.dumps({"n": name, "a": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def repeated_search_result(previous_summary: str) -> ToolResult:
    summary = previous_summary
    if len(summary) > REPEATED_SEARCH_SUMMARY_LIMIT:
        summary = summary[:REPEATED_SEARCH_SUMMARY_LIMIT] + "\n...[truncated]"
    payload = {
        "ok": True,
        "summary": (
            "search_information has already been called in this turn. "
            "Do not search again; answer the user now using the previous search evidence."
        ),
        "previous_search": summary,
    }
    return ToolResult(
        ok=True,
        summary=json.dumps(payload, ensure_ascii=False),
        outputs=[],
        console="",
        doc_links=[],
    )


def primary_artifact_kind(kinds: list[str]) -> str:
    for kind in kinds:
        normalized = str(kind or "").strip()
        if normalized in {"file", "directory"}:
            return normalized
    return ""


def paths_to_resources(paths: list[tuple[str, str]]) -> tuple[ResourceRef, ...]:
    out: list[ResourceRef] = []
    for path, kind in paths:
        if not path:
            continue
        name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or path
        resource_kind = cast(
            Literal["file", "directory", "url"],
            kind if kind in {"file", "directory", "url"} else "file",
        )
        out.append(ResourceRef(name=name, path=path, kind=resource_kind))
    return tuple(out)


__all__ = [
    "DEV_WRITE_TOOLS",
    "EMPTY_MODEL_REPLY_TEXT",
    "FINALIZE_SELF_UPDATE_TOOL",
    "SEARCH_INFORMATION_TOOL",
    "SELF_UPDATE_FINAL_TOOL_NAMES",
    "SELF_UPDATE_REQUIRED_PROMPT",
    "paths_to_resources",
    "primary_artifact_kind",
    "repeated_search_result",
    "safe_emit",
    "task_trace_id",
    "tool_fingerprint",
]
