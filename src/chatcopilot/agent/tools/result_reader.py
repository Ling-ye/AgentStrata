"""Session-owned, filtered result snapshots and their cataloged read tool."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from uuid import uuid4

from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
    tool_access_allowed,
)

PAGE_CHARS = 8_000
CACHE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _Snapshot:
    source: ToolDef
    text: str
    digest: str
    size: int


class SessionResultStore:
    """An actor-local LRU; contains only payload-filtered text, never file paths."""

    def __init__(
        self,
        permission_filter: Callable[[ToolDef], str | None] | None = None,
        *,
        max_bytes: int = CACHE_BYTES,
    ) -> None:
        self._permission_filter = permission_filter
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, _Snapshot] = OrderedDict()
        self._size = 0
        self._closed = False
        self._lock = threading.Lock()

    def project(self, tool: ToolDef, payload: dict[str, Any]) -> dict[str, Any]:
        """Project an explicitly expandable body after the host output filter."""
        field = tool.metadata.get("result_content_field")
        data = payload.get("data")
        if (
            payload.get("ok") is not True
            or not isinstance(field, str)
            or not isinstance(data, dict)
            or field not in data
        ):
            return payload
        value = data[field]
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
        encoded = text.encode("utf-8")
        # Do not lose evidence if an individual result cannot fit in the cache.
        if len(encoded) > self._max_bytes:
            return {**payload, "result_cache": "not_cached_oversized"}
        size = len(encoded) + len(tool.name.encode("utf-8")) + 256
        if size > self._max_bytes:
            return {**payload, "result_cache": "not_cached_oversized"}
        snapshot = _Snapshot(tool, text, hashlib.sha256(encoded).hexdigest(), size)
        with self._lock:
            if self._closed:
                return payload
            while self._entries and self._size + snapshot.size > self._max_bytes:
                _, old = self._entries.popitem(last=False)
                self._size -= old.size
            reference = "result_" + uuid4().hex
            self._entries[reference] = snapshot
            self._size += snapshot.size
        result = {
            **payload,
            "result_ref": {
                "id": reference,
                "reader": "read_tool_result",
                "source_tool": tool.name,
                "field": f"data.{field}",
                "sha256": snapshot.digest,
                "total_chars": len(text),
                "format": "text" if isinstance(value, str) else "json",
                "lifetime": "live_session_lru",
            },
        }
        if tool.metadata.get("result_inline_only") is True:
            result["result_inline_required"] = True
        elif len(text) > PAGE_CHARS:
            result["data"] = {key: item for key, item in data.items() if key != field}
            result["content_preview"] = text[:2_000]
            result["truncated"] = True
        return result

    def read(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        reference = str(args.get("result_id", ""))
        with self._lock:
            snapshot = self._entries.get(reference)
            if snapshot is not None:
                self._entries.move_to_end(reference)
        if snapshot is None:
            return ToolResult(
                ok=False,
                error="结果引用不存在或已随会话/缓存保留期失效；未重新执行原工具。",
                error_code="tool_result_unavailable",
                stage="result_read",
            )
        denied = (
            self._permission_filter(snapshot.source)
            if self._permission_filter is not None
            else None
            if tool_access_allowed(ctx.caller_role, snapshot.source.access)
            else "无权读取原工具结果。"
        )
        if denied:
            return ToolResult(
                ok=False, error=denied, error_code="tool_permission_denied", stage="permission"
            )
        offset = int(args.get("offset", 0))
        limit = int(args.get("limit", 4_000))
        if offset < 0 or offset > len(snapshot.text) or not 1 <= limit <= PAGE_CHARS:
            return ToolResult(
                ok=False,
                error="字符范围无效。",
                error_code="tool_result_range_invalid",
                stage="result_read",
            )
        query = str(args.get("query", ""))
        if query:
            offset = snapshot.text.find(query, offset)
            if offset < 0:
                return ToolResult(
                    ok=True,
                    summary="当前结果在指定位置之后未找到该文字。",
                    data={"result_id": reference, "found": False, "sha256": snapshot.digest},
                )
        end = min(offset + limit, len(snapshot.text))
        return ToolResult(
            ok=True,
            summary=f"已读取结果 {reference}（{snapshot.source.name}）字符 {offset}–{end}；共 {len(snapshot.text)} 字符。",
            data={
                "result_id": reference,
                "source_tool": snapshot.source.name,
                "sha256": snapshot.digest,
                "found": True,
                "text": snapshot.text[offset:end],
                "offset": offset,
                "next_offset": end if end < len(snapshot.text) else None,
                "total_chars": len(snapshot.text),
            },
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._entries.clear()
            self._size = 0


def result_reader_provider(store: SessionResultStore) -> ToolProvider:
    tool = ToolDef(
        name="read_tool_result",
        summary="分页回读当前 Agent 会话的结果快照。预览不是完整证据；需要流程或原文时使用 result_ref.id。offset 从零开始，query 定位文字。会话关闭或缓存淘汰后引用失效，应说明缺口，不自动重放原工具或写操作。",
        input_schema=object_schema(
            {
                "result_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_CHARS},
                "query": {"type": "string"},
            },
            required=("result_id",),
        ),
        output_schema=object_schema(
            {
                "result_id": {"type": "string"},
                "source_tool": {"type": "string"},
                "sha256": {"type": "string"},
                "found": {"type": "boolean"},
                "text": {"type": "string"},
                "offset": {"type": "integer"},
                "next_offset": {"type": ["integer", "null"]},
                "total_chars": {"type": "integer"},
            }
        ),
        handler=store.read,
        access="member",
        category="context.results",
        owner="agent",
        module=__name__,
        audiences=("main",),
    )
    return ToolProvider(id="context_results", packs={"context.results": (tool,)}, module=__name__)


TOOL_PROVIDER = result_reader_provider(SessionResultStore())
