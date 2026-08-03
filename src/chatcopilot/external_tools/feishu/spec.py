"""通用飞书工具 ToolDef 声明（机器人入口）。

只负责：ToolDef 声明、参数校验/归一化、调用 Service、给 bot 的回复格式化。
真实业务在 ``service.py`` / ``modules/``。

安全约束：所有 mutating 动作（建档/写表/改记录/发消息）通过 ``requires_role="owner"``
交给 middleware 的 permission_filter 拦截——非 owner 既看不到也调不动；只读工具
（读表/查记录/检索/api-get）对所有人开放。``feishu_im_send`` 强制显式接收者 ID。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from chatcopilot.external_tools.shared.spec_helpers import require_arg, schema_property
from chatcopilot.external_tools.shared.tool_spec import HandlerResult, ToolDef

from chatcopilot.external_tools.feishu.service import FeishuService

_CATEGORY = "feishu"
_OWNER = "feishu"
_SERVICE = FeishuService()


def _feishu_tool(*, requires_role: str | None = None, **kwargs: Any) -> ToolDef:
    return ToolDef(
        category=_CATEGORY,
        owner=_OWNER,
        module=__name__,
        weight="heavy",
        requires_role=requires_role,
        **kwargs,
    )


def _as_2d_values(raw: Any) -> List[List[Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"values 不是合法 JSON: {exc}")
    if not isinstance(raw, list) or not raw or not all(isinstance(r, list) for r in raw):
        raise ValueError("values 必须是非空二维数组，例如 [[\"A\",\"B\"],[1,2]]")
    return raw


def _as_object(raw: Any, *, name: str) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} 不是合法 JSON: {exc}")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{name} 必须是非空对象")
    return raw


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------
def _h_doc_create(args: Dict[str, Any]) -> HandlerResult:
    title = require_arg(args, "title")
    return _SERVICE.create_doc(title=title, markdown=str(args.get("markdown") or "")).to_handler_result()


def _h_doc_append(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    markdown = require_arg(args, "markdown")
    return _SERVICE.append_doc(url=url, markdown=markdown).to_handler_result()


def _h_sheet_read(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    return _SERVICE.read_sheet(
        url=url, range_a1=str(args.get("range") or ""), sheet_id=str(args.get("sheet_id") or ""),
    ).to_handler_result()


def _h_sheet_write(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    values = _as_2d_values(args.get("values"))
    return _SERVICE.write_sheet(
        url=url, values=values, range_a1=str(args.get("range") or ""), sheet_id=str(args.get("sheet_id") or ""),
    ).to_handler_result()


def _h_sheet_append(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    values = _as_2d_values(args.get("values"))
    return _SERVICE.append_sheet(
        url=url, values=values, range_a1=str(args.get("range") or ""), sheet_id=str(args.get("sheet_id") or ""),
    ).to_handler_result()


def _h_bitable_query(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    return _SERVICE.bitable_query(
        url=url, table_id=str(args.get("table_id") or ""),
        page_size=int(args.get("page_size") or 20), view_id=str(args.get("view_id") or ""),
    ).to_handler_result()


def _h_bitable_add(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    fields = _as_object(args.get("fields"), name="fields")
    return _SERVICE.bitable_add(url=url, fields=fields, table_id=str(args.get("table_id") or "")).to_handler_result()


def _h_bitable_update(args: Dict[str, Any]) -> HandlerResult:
    url = require_arg(args, "url")
    record_id = require_arg(args, "record_id")
    fields = _as_object(args.get("fields"), name="fields")
    return _SERVICE.bitable_update(
        url=url, record_id=record_id, fields=fields, table_id=str(args.get("table_id") or ""),
    ).to_handler_result()


def _h_wiki_search(args: Dict[str, Any]) -> HandlerResult:
    query = require_arg(args, "query")
    return _SERVICE.wiki_search(
        query=query, space_id=str(args.get("space_id") or ""), page_size=int(args.get("page_size") or 20),
    ).to_handler_result()


def _h_drive_search(args: Dict[str, Any]) -> HandlerResult:
    query = require_arg(args, "query")
    return _SERVICE.drive_search(query=query, count=int(args.get("count") or 20)).to_handler_result()


def _h_im_send(args: Dict[str, Any]) -> HandlerResult:
    receive_id = require_arg(args, "receive_id")
    receive_id_type = require_arg(args, "receive_id_type")
    return _SERVICE.send_message(
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        msg_type=str(args.get("msg_type") or "text"),
        text=str(args.get("text") or ""),
        content=str(args.get("content") or ""),
    ).to_handler_result()


def _h_api_get(args: Dict[str, Any]) -> HandlerResult:
    path = require_arg(args, "path")
    params = args.get("params")
    if isinstance(params, str) and params.strip():
        params = _as_object(params, name="params")
    elif not isinstance(params, dict):
        params = None
    return _SERVICE.api_get(path=path, params=params).to_handler_result()


# ----------------------------------------------------------------------------
# Tool declarations
# ----------------------------------------------------------------------------
_VALUES_PROP = {
    "type": "array",
    "description": "二维数组，每个子数组是一行，如 [[\"姓名\",\"分数\"],[\"张三\",90]]",
    "items": {"type": "array"},
}
_FIELDS_PROP = {
    "type": "object",
    "description": "记录字段对象 {字段名: 值}，字段名需与多维表格列名一致",
}

TOOLS: List[ToolDef] = [
    _feishu_tool(
        name="feishu_doc_create",
        summary="新建飞书云文档（标题 + Markdown 正文），返回文档链接。仅 owner 可用。",
        properties={
            "title": schema_property(type="string", description="文档标题"),
            "markdown": schema_property(type="string", description="Markdown 正文，可为空"),
        },
        required=["title"],
        handler=_h_doc_create,
        aliases=["创建飞书文档", "新建飞书文档", "lark_doc_create"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_doc_append",
        summary="向已有飞书 docx 文档末尾追加 Markdown 文本（按行追加为段落）。仅 owner 可用。",
        properties={
            "url": schema_property(type="string", description="飞书 docx 文档 URL"),
            "markdown": schema_property(type="string", description="要追加的 Markdown 文本"),
        },
        required=["url", "markdown"],
        handler=_h_doc_append,
        aliases=["追加飞书文档", "lark_doc_append"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_sheet_read",
        summary="读取飞书电子表格指定范围的数据（返回二维数组预览）。",
        properties={
            "url": schema_property(type="string", description="飞书电子表格 URL"),
            "range": schema_property(type="string", description="范围，如 A1:D20；不带页签时用 sheet_id 或 URL ?sheet="),
            "sheet_id": schema_property(type="string", description="页签 ID，可选"),
        },
        required=["url"],
        handler=_h_sheet_read,
        aliases=["读取飞书表格", "lark_sheet_read"],
    ),
    _feishu_tool(
        name="feishu_sheet_write",
        summary="覆盖写入飞书电子表格指定范围。仅 owner 可用。",
        properties={
            "url": schema_property(type="string", description="飞书电子表格 URL"),
            "values": _VALUES_PROP,
            "range": schema_property(type="string", description="写入范围，如 A1:D3"),
            "sheet_id": schema_property(type="string", description="页签 ID，可选"),
        },
        required=["url", "values"],
        handler=_h_sheet_write,
        aliases=["写入飞书表格", "lark_sheet_write"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_sheet_append",
        summary="向飞书电子表格末尾追加行。仅 owner 可用。",
        properties={
            "url": schema_property(type="string", description="飞书电子表格 URL"),
            "values": _VALUES_PROP,
            "range": schema_property(type="string", description="参考范围，默认 A1，可选"),
            "sheet_id": schema_property(type="string", description="页签 ID，可选"),
        },
        required=["url", "values"],
        handler=_h_sheet_append,
        aliases=["追加飞书表格", "lark_sheet_append"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_bitable_query",
        summary="查询飞书多维表格（Bitable）记录（分页）。",
        properties={
            "url": schema_property(type="string", description="飞书多维表格 URL（含 ?table=）"),
            "table_id": schema_property(type="string", description="table_id，URL 未带 ?table= 时必填"),
            "page_size": schema_property(type="integer", description="每页记录数，默认 20", default=20),
            "view_id": schema_property(type="string", description="视图 ID，可选"),
        },
        required=["url"],
        handler=_h_bitable_query,
        aliases=["查询多维表格", "lark_bitable_query"],
    ),
    _feishu_tool(
        name="feishu_bitable_add",
        summary="向飞书多维表格新增一条记录。仅 owner 可用。",
        properties={
            "url": schema_property(type="string", description="飞书多维表格 URL（含 ?table=）"),
            "fields": _FIELDS_PROP,
            "table_id": schema_property(type="string", description="table_id，URL 未带 ?table= 时必填"),
        },
        required=["url", "fields"],
        handler=_h_bitable_add,
        aliases=["新增多维表格记录", "lark_bitable_add"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_bitable_update",
        summary="更新飞书多维表格指定记录的字段。仅 owner 可用。",
        properties={
            "url": schema_property(type="string", description="飞书多维表格 URL（含 ?table=）"),
            "record_id": schema_property(type="string", description="要更新的 record_id"),
            "fields": _FIELDS_PROP,
            "table_id": schema_property(type="string", description="table_id，URL 未带 ?table= 时必填"),
        },
        required=["url", "record_id", "fields"],
        handler=_h_bitable_update,
        aliases=["更新多维表格记录", "lark_bitable_update"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_wiki_search",
        summary="在飞书知识库节点中检索关键词（可见范围受应用权限/共享范围限制）。",
        properties={
            "query": schema_property(type="string", description="检索关键词"),
            "space_id": schema_property(type="string", description="限定知识空间 ID，可选"),
            "page_size": schema_property(type="integer", description="返回条数，默认 20", default=20),
        },
        required=["query"],
        handler=_h_wiki_search,
        aliases=["检索知识库", "lark_wiki_search"],
    ),
    _feishu_tool(
        name="feishu_drive_search",
        summary="在飞书云盘文档中按关键词检索（可见范围受应用权限/共享范围限制）。",
        properties={
            "query": schema_property(type="string", description="检索关键词"),
            "count": schema_property(type="integer", description="返回条数，默认 20", default=20),
        },
        required=["query"],
        handler=_h_drive_search,
        aliases=["检索云盘文档", "lark_drive_search"],
    ),
    _feishu_tool(
        name="feishu_im_send",
        summary=(
            "以应用(机器人)身份给指定飞书用户/群发送消息，必须显式提供接收者 ID。"
            "仅 owner 可用，避免误发。"
        ),
        properties={
            "receive_id": schema_property(type="string", description="接收者 ID（必填，显式指定）"),
            "receive_id_type": schema_property(
                type="string",
                description="ID 类型",
                enum=["open_id", "user_id", "union_id", "email", "chat_id"],
            ),
            "msg_type": schema_property(type="string", description="消息类型，默认 text", default="text"),
            "text": schema_property(type="string", description="文本消息内容（msg_type=text 时）"),
            "content": schema_property(type="string", description="原始 content JSON 字符串（post/卡片等高级用法）"),
        },
        required=["receive_id", "receive_id_type"],
        handler=_h_im_send,
        aliases=["发送飞书消息", "lark_im_send"],
        requires_role="owner",
    ),
    _feishu_tool(
        name="feishu_api_get",
        summary=(
            "只读逃生门：以应用身份发起任意飞书 OpenAPI GET 请求（仅 GET）。"
            "用于 curated 工具未覆盖的查询场景，如 /im/v1/chats、/contact/v3/users 等。"
        ),
        properties={
            "path": schema_property(type="string", description="OpenAPI 路径，以 / 开头，如 /im/v1/chats"),
            "params": schema_property(type="string", description="query 参数 JSON 字符串，可选"),
        },
        required=["path"],
        handler=_h_api_get,
        aliases=["飞书API查询", "lark_api_get"],
    ),
]


__all__ = ["TOOLS"]
