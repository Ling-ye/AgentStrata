"""Tool definitions for the owner-private local Markdown Wiki."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.contracts.workspace import normalize_chat_kind
from chatcopilot.core.wiki import WikiStore

_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}


def _store() -> WikiStore:
    return WikiStore.from_env()


def _require_private_context(ctx: ToolContext) -> None:
    workspace = ctx.workspace
    chat_kind = normalize_chat_kind(
        getattr(workspace, "chat_kind", None), getattr(workspace, "chat_id", None)
    )
    if workspace is None or chat_kind != "p2p":
        raise PermissionError("私有 Wiki 仅允许在 owner 私聊中读写")


def _list_arg(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def _result(payload: Any) -> ToolResult:
    data = payload if isinstance(payload, dict) else {"result": payload}
    return ToolResult(ok=True, summary="私有 Wiki 操作完成。", data=data)


def _upsert(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    _require_private_context(ctx)
    result = _store().upsert_page(
        title=str(args.get("title") or ""),
        summary=str(args.get("summary") or ""),
        facts=_list_arg(args, "facts"),
        procedures=_list_arg(args, "procedures"),
        open_questions=_list_arg(args, "open_questions"),
        tags=_list_arg(args, "tags"),
        source_text=str(args.get("source_text") or ""),
        source_kind=str(args.get("source_kind") or "chat"),
        source_ref=str(args.get("source_ref") or ""),
        target_path=str(args.get("target_path") or ""),
    )
    return _result(asdict(result))


def _search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    _require_private_context(ctx)
    hits = _store().search(
        str(args.get("query") or ""),
        top_k=int(args.get("top_k") or 5),
    )
    return _result({"hits": [asdict(hit) for hit in hits]})


def _read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    _require_private_context(ctx)
    page, body = _store().read_page(str(args.get("path") or ""))
    return _result({"page": asdict(page), "body": body})


def _list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    _require_private_context(ctx)
    pages = _store().list_pages(limit=int(args.get("limit") or 50))
    return _result({"pages": [asdict(page) for page in pages]})


_PRIVATE_METADATA = {"tags": ["wiki", "private"], "private_chat_only": True}
_WIKI_RESULT_SCHEMA = {"type": "object", "additionalProperties": True}

TOOLS = [
    ToolDef(
        name="wiki_upsert_page",
        summary=(
            "把 owner 明确要求保存的原始文本或 Markdown 写入私有 Wiki。"
            "先忠实传入 source_text，再将内容整理为摘要、事实、步骤和待确认项；"
            "不要补造来源中不存在的事实。"
        ),
        input_schema=object_schema({
            "title": {"type": "string", "description": "页面标题。"},
            "summary": {"type": "string", "description": "忠实、简洁的内容摘要。"},
            "facts": {**_STRING_ARRAY, "description": "来源明确支持的事实；至少一项。"},
            "procedures": {**_STRING_ARRAY, "description": "步骤、决策或操作方法；可为空。"},
            "open_questions": {**_STRING_ARRAY, "description": "不确定、冲突或待确认事项；可为空。"},
            "tags": {**_STRING_ARRAY, "description": "用于检索的少量标签。"},
            "source_text": {"type": "string", "description": "用户提供的原始文本或 Markdown，保持原意。"},
            "source_kind": {
                "type": "string",
                "enum": ["chat", "text", "markdown"],
                "description": "原始来源类型。",
                "default": "chat",
            },
            "source_ref": {
                "type": "string",
                "description": "可选稳定来源标识；同一标识内容变化时更新原页面。",
            },
            "target_path": {
                "type": "string",
                "description": "可选 pages/ 内相对 Markdown 路径；仅在明确合并或更新指定页面时传入。",
            },
        }, required=("title", "summary", "facts", "source_text")),
        output_schema=_WIKI_RESULT_SCHEMA,
        handler=_upsert,
        requires_role="owner",
        category="wiki.knowledge",
        owner="wiki",
        module=__name__,
        metadata={**_PRIVATE_METADATA, "tags": ["wiki", "private", "write"]},
    ),
    ToolDef(
        name="wiki_search",
        summary="搜索 owner 私有 Wiki，返回页面、章节、来源标识和相关片段。",
        input_schema=object_schema({
            "query": {"type": "string", "description": "检索问题或关键词。"},
            "top_k": {"type": "integer", "description": "返回结果数，最多 20。", "default": 5},
        }, required=("query",)),
        output_schema=_WIKI_RESULT_SCHEMA,
        handler=_search,
        requires_role="owner",
        category="wiki.knowledge",
        owner="wiki",
        module=__name__,
        metadata=_PRIVATE_METADATA,
    ),
    ToolDef(
        name="wiki_read_page",
        summary="读取 owner 私有 Wiki 中一个 Markdown 页面的正文和来源元数据。",
        input_schema=object_schema(
            {"path": {"type": "string", "description": "pages/ 内相对 Markdown 路径。"}},
            required=("path",),
        ),
        output_schema=_WIKI_RESULT_SCHEMA,
        handler=_read,
        requires_role="owner",
        category="wiki.knowledge",
        owner="wiki",
        module=__name__,
        metadata=_PRIVATE_METADATA,
    ),
    ToolDef(
        name="wiki_list_pages",
        summary="列出 owner 私有 Wiki 页面及其标题、标签、更新时间和来源标识。",
        input_schema=object_schema(
            {"limit": {"type": "integer", "description": "返回页面数，最多 200。", "default": 50}}
        ),
        output_schema=_WIKI_RESULT_SCHEMA,
        handler=_list,
        requires_role="owner",
        category="wiki.knowledge",
        owner="wiki",
        module=__name__,
        metadata=_PRIVATE_METADATA,
    ),
]

TOOL_PROVIDER = static_tool_provider(
    "wiki",
    packs={"wiki.knowledge": tuple(TOOLS)},
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
