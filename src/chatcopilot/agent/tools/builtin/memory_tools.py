"""Conversation-scoped memory tools backed by trusted persistent state."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List

from chatcopilot.agent.trace import current_trace
from chatcopilot.agent.tools.workspace_context import resolve_persistent_state
from chatcopilot.contracts.persistent_state import MEMORY_MAX_ITEM_CHARS, MEMORY_SECTIONS
from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema
from chatcopilot.core.memory_intent import classify_memory_intent
from chatcopilot.core.memory_policy import evaluate_memory_content
from chatcopilot.external_tools.shared.spec_helpers import require_arg


def _persistent_state(ctx: ToolContext | None):
    state = ctx.persistent_state if ctx is not None else None
    return state if state is not None else resolve_persistent_state()


def _caller_is_owner(ctx: ToolContext | None) -> bool:
    role = str(ctx.caller_role if ctx is not None else "").strip().lower()
    if not role:
        from chatcopilot.core.caller_context import get_caller_role_hint

        role = get_caller_role_hint()
    return role == "owner"


def _contains_request_span(request: str, text: str) -> bool:
    def compact(value: str) -> str:
        return re.sub(r"[\s。.!?！？]+", "", value or "").casefold()
    candidate = compact(text)
    return bool(candidate) and candidate in compact(request)


def _append_request_bound(request: str, text: str) -> bool:
    match = re.search(
        r"(?:请)?记住|记下来|写入记忆|保存(?:下来|为偏好|到记忆)|以后按(?:这个|此|这条)",
        request,
        re.IGNORECASE,
    )
    if match is None:
        return False
    tail = request[match.end():].lstrip(" ：:，,")
    if re.match(r"(?:这个变化|这一点|这件事)", tail):
        return _contains_request_span(request[:match.start()], text)
    tail = re.split(r"[。；;\n]|[，,](?=\s*(?:不要|别|禁止))", tail, maxsplit=1)[0]
    return _contains_request_span(tail, text)


def _record_payload(record) -> dict[str, Any]:
    return {
        "item_id": record.item_id,
        "text": record.text,
        "section": record.section,
        "version": record.version,
        "origin": record.origin,
        "updated_at": record.updated_at,
        "status": record.status,
    }


def _handler_read_memory(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    state = _persistent_state(ctx)
    item_id = str(args.get("item_id") or "").strip()
    query = str(args.get("query") or "").strip()
    if item_id and query:
        return ToolResult(ok=False, error="item_id 与 query 只能选择一个", stage="validation")
    if item_id:
        record = state.memory_read(item_id)
        if record is None or record.status != "active":
            return ToolResult(ok=False, error="当前作用域没有该有效记忆", stage="lookup")
        items = [_record_payload(record)]
    else:
        items = [_record_payload(item) for item in state.memory_search(query, limit=5)]
    text = "\n".join(f"[{item['item_id']}] {item['text']}" for item in items)
    return ToolResult(
        ok=True,
        summary=(f"当前 {state.memory_scope} 作用域长期记忆：\n{text}" if items
                 else f"当前 {state.memory_scope} 作用域尚无长期记忆或无匹配结果。"),
        data={"scope": state.memory_scope, "text": text, "has_memory": bool(items), "items": items},
    )


def _handler_append_memory(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    memory_text = require_arg(args, "text").strip()
    section = (args.get("section") or "facts").strip() or "facts"
    state = _persistent_state(ctx)
    if ctx.request_text and classify_memory_intent(ctx.request_text) == "append":
        request_decision = evaluate_memory_content(
            ctx.request_text, scope=state.memory_scope
        )
        if not request_decision.allowed:
            return ToolResult(
                ok=False,
                error=f"拒绝写入：{request_decision.reason}",
                error_code="memory_content_rejected",
                stage="validation",
            )
        if not _append_request_bound(ctx.request_text, memory_text):
            return ToolResult(
                ok=False,
                error="写入正文不是当前明确记忆请求中的原文片段",
                error_code="memory_request_content_mismatch",
                stage="validation",
            )
    decision = evaluate_memory_content(memory_text, scope=state.memory_scope)
    if not decision.allowed:
        return ToolResult(
            ok=False,
            error=f"拒绝写入：{decision.reason}",
            error_code="memory_content_rejected",
            stage="validation",
        )
    trace = current_trace()
    receipt = state.memory_append(
        text=memory_text,
        section=section,
        source_turn=trace.trace_id if trace is not None else "",
    )
    summary = (
        f"已写入当前 {receipt.scope} 作用域长期记忆。"
        if receipt.created else
        f"当前 {receipt.scope} 作用域已存在完全相同的记忆，未重复写入。"
    )
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "action": "append",
            "scope": receipt.scope,
            "item_id": receipt.item_id,
            "version": receipt.version,
            "content_sha256": receipt.content_sha256,
            "request_bound": _append_request_bound(ctx.request_text, memory_text),
            "created": receipt.created,
            "section": section,
        },
    )


def _handler_clear_memory(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not bool(args.get("confirm", False)):
        return ToolResult(
            ok=False,
            error="拒绝清空：clear_memory 需要 confirm=true 才会执行。",
            error_code="memory_clear_confirmation_required",
            stage="validation",
        )
    state = _persistent_state(ctx)
    if not _caller_is_owner(ctx):
        return ToolResult(
            ok=False,
            error="只有 Owner 可以清空会话记忆。",
            error_code="memory_clear_owner_required",
            stage="permission",
        )
    if classify_memory_intent(ctx.request_text) != "clear":
        return ToolResult(
            ok=False,
            error="当前用户请求并未明确要求清空整份记忆。",
            error_code="memory_clear_intent_missing",
            stage="validation",
        )
    state.memory_clear()
    return ToolResult(
        ok=True,
        summary=f"当前 {state.memory_scope} 作用域长期记忆已清空。",
        data={
            "action": "clear", "scope": state.memory_scope, "cleared": True,
            "request_bound": classify_memory_intent(ctx.request_text) == "clear",
        },
    )


def _handler_manage_memory(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not _caller_is_owner(ctx):
        return ToolResult(
            ok=False, error="只有 Owner 可以更正或删除记忆。",
            error_code="memory_manage_owner_required", stage="permission",
        )
    action = require_arg(args, "action")
    item_id = require_arg(args, "item_id")
    query = require_arg(args, "query").strip()
    version = args.get("expected_version")
    state = _persistent_state(ctx)
    if type(version) is not int or version < 1 or not query:
        return ToolResult(ok=False, error="必须指定目标查询和有效版本", stage="validation")
    if not ctx.request_text:
        return ToolResult(ok=False, error="缺少当前可信用户请求，拒绝修改记忆", stage="validation")
    record = state.memory_read(item_id)
    if record is None or record.status != "active" or record.version != version:
        return ToolResult(ok=False, error="目标记忆不存在或版本已变化", stage="lookup")
    if ctx.request_text and not _contains_request_span(ctx.request_text, query):
        return ToolResult(ok=False, error="目标查询未绑定当前用户请求", stage="validation")
    matches = state.memory_search(query, limit=2)
    if len(matches) != 1 or matches[0].item_id != item_id:
        return ToolResult(
            ok=False, error="目标记忆不唯一；请先列出候选并缩小查询",
            error_code="memory_target_ambiguous", stage="lookup",
            data={"candidates": [_record_payload(item) for item in matches]},
        )
    if action == "delete":
        if ctx.request_text and classify_memory_intent(ctx.request_text) != "targeted_forget":
            return ToolResult(ok=False, error="当前请求未明确要求删除单条记忆", stage="validation")
        if not state.memory_delete(item_id, expected_version=version):
            return ToolResult(ok=False, error="目标记忆版本已变化", stage="lookup")
        return ToolResult(
            ok=True,
            summary="当前作用域的指定记忆已删除。",
            data={
                "action": "delete", "scope": state.memory_scope, "item_id": item_id,
                "version": version + 1,
                "content_sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
                "request_bound": classify_memory_intent(ctx.request_text) == "targeted_forget",
            },
        )
    if action == "update":
        text = require_arg(args, "text").strip()
        if ctx.request_text and not _contains_request_span(ctx.request_text, text):
            return ToolResult(ok=False, error="新正文未绑定当前用户请求", stage="validation")
        trace = current_trace()
        updated = state.memory_update(
            item_id, text=text, expected_version=version,
            source_turn=trace.trace_id if trace is not None else "",
        )
        return ToolResult(
            ok=True,
            summary="当前作用域的指定记忆已更正。",
            data={
                "action": "update", "scope": state.memory_scope, "item_id": item_id,
                "version": updated.version,
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "request_bound": _contains_request_span(ctx.request_text, text),
            },
        )
    return ToolResult(ok=False, error="action 只能是 update 或 delete", stage="validation")


_MEMORY_READ_RESULT_SCHEMA = object_schema(
    {
        "scope": {"type": "string"},
        "text": {"type": "string"},
        "has_memory": {"type": "boolean"},
        "items": {"type": "array", "items": {"type": "object"}},
    },
    required=("scope", "text", "has_memory", "items"),
)

TOOLS: List[ToolDef] = [
    ToolDef(
        access="member",
        name="read_memory",
        disclosure="direct",
        summary="按当前可信群或私聊身份读取记忆。可按关键词查询或条目 ID 读取，不能指定其它身份。",
        input_schema=object_schema({
            "query": {"type": "string", "description": "本地关键词或短语"},
            "item_id": {"type": "string", "description": "上次查询返回的条目 ID"},
        }),
        output_schema=_MEMORY_READ_RESULT_SCHEMA,
        handler=_handler_read_memory,
        aliases=["mem", "查看记忆"],
        category="agent.memory", owner="agent", module=__name__,
    ),
    ToolDef(
        access="member",
        name="append_memory",
        disclosure="direct",
        summary=(
            "保存当前发言中未来可复用的原文片段到当前可信群或私聊记忆。"
            "明确要求记住时立即调用；不要保存秘密、群内个人隐私、临时任务、"
            "人格或权限指令。自动提炼由宿主处理。"
        ),
        input_schema=object_schema(
            {
                "text": {
                    "type": "string",
                    "description": f"当前用户原文中的可复用片段，不超过 {MEMORY_MAX_ITEM_CHARS} 字符。",
                },
                "section": {
                    "type": "string",
                    "enum": list(MEMORY_SECTIONS),
                    "default": "facts",
                },
            },
            required=("text",),
        ),
        output_schema=object_schema(
            {
                "action": {"type": "string"},
                "scope": {"type": "string"},
                "item_id": {"type": "string"},
                "version": {"type": "integer"},
                "content_sha256": {"type": "string"},
                "request_bound": {"type": "boolean"},
                "created": {"type": "boolean"},
                "section": {"type": "string"},
            },
            required=("action", "scope", "item_id", "version", "content_sha256", "request_bound", "created", "section"),
        ),
        handler=_handler_append_memory,
        aliases=["记下", "remember"],
        category="agent.memory", owner="agent", module=__name__,
    ),
    ToolDef(
        name="manage_memory",
        disclosure="direct",
        summary="仅 Owner 更正或删除当前作用域的唯一匹配条目；先查询取得 ID 和版本。",
        input_schema=object_schema(
            {
                "action": {"type": "string", "enum": ["update", "delete"]},
                "item_id": {"type": "string"},
                "query": {"type": "string"},
                "expected_version": {"type": "integer"},
                "text": {"type": "string"},
            },
            required=("action", "item_id", "query", "expected_version"),
        ),
        output_schema=object_schema(
            {
                "action": {"type": "string"}, "scope": {"type": "string"},
                "item_id": {"type": "string"}, "version": {"type": "integer"},
                "content_sha256": {"type": "string"},
                "request_bound": {"type": "boolean"},
            },
            required=("action", "scope", "item_id", "version", "content_sha256", "request_bound"),
        ),
        handler=_handler_manage_memory,
        category="agent.memory", owner="agent", module=__name__,
    ),
    ToolDef(
        name="clear_memory",
        disclosure="direct",
        summary="仅 Owner 在明确要求清空全部当前作用域记忆时使用；单条忘记必须使用 manage_memory。",
        input_schema=object_schema(
            {"confirm": {"type": "boolean", "default": False}}, required=("confirm",)
        ),
        output_schema=object_schema(
            {
                "action": {"type": "string"}, "scope": {"type": "string"},
                "cleared": {"type": "boolean"}, "request_bound": {"type": "boolean"},
            },
            required=("action", "scope", "cleared", "request_bound"),
        ),
        handler=_handler_clear_memory,
        aliases=["重置全部记忆"],
        category="agent.memory", owner="agent", module=__name__,
    ),
]

TOOL_PROVIDER = static_tool_provider(
    "memory", packs={"memory.chat": tuple(TOOLS)}, module=__name__
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
