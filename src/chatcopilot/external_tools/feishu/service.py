"""通用飞书能力的唯一业务门面（Service）。

机器人 spec / CLI 都只调本门面，不直接 import modules。所有方法返回
``ToolServiceResult``（transport-neutral），由调用方决定如何呈现。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from chatcopilot.external_tools.shared.lark_cli import run_api
from chatcopilot.external_tools.shared.service_contracts import ToolServiceResult, to_jsonable

from chatcopilot.external_tools.feishu.modules import bitable, docs, im, search, sheets


def _clip(text: str, limit: int = 1500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n...（已截断，原文约 {len(text)} 字符）"


def _extract_values(payload: Dict[str, Any]) -> List[List[Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    value_range = data.get("valueRange", {}) if isinstance(data, dict) else {}
    values = value_range.get("values", []) if isinstance(value_range, dict) else []
    return values if isinstance(values, list) else []


class FeishuService:
    """通用飞书（lark-cli ``--as bot``）能力门面。"""

    # ---- docs ---------------------------------------------------------------
    def create_doc(self, *, title: str, markdown: str = "") -> ToolServiceResult:
        info = docs.create_doc(title=title, markdown=markdown)
        url = info.get("url", "")
        message = f"已创建飞书云文档：{info.get('title', title)}"
        if url:
            message += f"\n链接：{url}"
        elif info.get("document_id"):
            message += f"\ndocument_id：{info['document_id']}"
        return ToolServiceResult(message=message, outputs=[], data=to_jsonable(info))

    def append_doc(self, *, url: str, markdown: str) -> ToolServiceResult:
        payload = docs.append_markdown(url=url, markdown=markdown)
        return ToolServiceResult(
            message=f"已向文档追加内容：{url}",
            outputs=[],
            data=to_jsonable(payload),
        )

    # ---- sheets -------------------------------------------------------------
    def read_sheet(self, *, url: str, range_a1: str = "", sheet_id: str = "") -> ToolServiceResult:
        payload = sheets.read_range(url, range_a1=range_a1, sheet_id=sheet_id)
        values = _extract_values(payload)
        preview = _clip(json.dumps(values[:20], ensure_ascii=False))
        return ToolServiceResult(
            message=f"已读取表格 {len(values)} 行。\n预览：{preview}",
            outputs=[],
            data=to_jsonable(payload),
        )

    def write_sheet(
        self, *, url: str, values: List[List[Any]], range_a1: str = "", sheet_id: str = ""
    ) -> ToolServiceResult:
        payload = sheets.write_range(url, values=values, range_a1=range_a1, sheet_id=sheet_id)
        return ToolServiceResult(
            message=f"已写入表格 {len(values)} 行。",
            outputs=[],
            data=to_jsonable(payload),
        )

    def append_sheet(
        self, *, url: str, values: List[List[Any]], range_a1: str = "", sheet_id: str = ""
    ) -> ToolServiceResult:
        payload = sheets.append_rows(url, values=values, range_a1=range_a1, sheet_id=sheet_id)
        return ToolServiceResult(
            message=f"已向表格追加 {len(values)} 行。",
            outputs=[],
            data=to_jsonable(payload),
        )

    # ---- bitable ------------------------------------------------------------
    def bitable_query(
        self, *, url: str, table_id: str = "", page_size: int = 20, view_id: str = ""
    ) -> ToolServiceResult:
        payload = bitable.list_records(url, table_id=table_id, page_size=page_size, view_id=view_id)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        preview = _clip(json.dumps(items[:10], ensure_ascii=False))
        return ToolServiceResult(
            message=f"多维表格命中 {len(items)} 条记录（本页）。\n预览：{preview}",
            outputs=[],
            data=to_jsonable(payload),
        )

    def bitable_add(self, *, url: str, fields: Dict[str, Any], table_id: str = "") -> ToolServiceResult:
        payload = bitable.add_record(url, fields=fields, table_id=table_id)
        rec = payload.get("data", {}).get("record", {}) if isinstance(payload, dict) else {}
        record_id = rec.get("record_id", "") if isinstance(rec, dict) else ""
        return ToolServiceResult(
            message=f"已新增多维表格记录。record_id={record_id}",
            outputs=[],
            data=to_jsonable(payload),
        )

    def bitable_update(
        self, *, url: str, record_id: str, fields: Dict[str, Any], table_id: str = ""
    ) -> ToolServiceResult:
        payload = bitable.update_record(url, record_id=record_id, fields=fields, table_id=table_id)
        return ToolServiceResult(
            message=f"已更新多维表格记录 {record_id}。",
            outputs=[],
            data=to_jsonable(payload),
        )

    # ---- search -------------------------------------------------------------
    def wiki_search(self, *, query: str, space_id: str = "", page_size: int = 20) -> ToolServiceResult:
        payload = search.wiki_search(query=query, space_id=space_id, page_size=page_size)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        items = data.get("items", data.get("nodes", [])) if isinstance(data, dict) else []
        return ToolServiceResult(
            message=f"知识库检索 “{query}” 命中 {len(items)} 条。",
            outputs=[],
            data=to_jsonable(payload),
        )

    def drive_search(self, *, query: str, count: int = 20) -> ToolServiceResult:
        payload = search.drive_search(query=query, count=count)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        entities = data.get("docs_entities", data.get("entities", [])) if isinstance(data, dict) else []
        return ToolServiceResult(
            message=f"云盘检索 “{query}” 命中 {len(entities)} 条。",
            outputs=[],
            data=to_jsonable(payload),
        )

    # ---- im -----------------------------------------------------------------
    def send_message(
        self,
        *,
        receive_id: str,
        receive_id_type: str,
        msg_type: str = "text",
        text: str = "",
        content: str = "",
    ) -> ToolServiceResult:
        payload = im.send_message(
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            msg_type=msg_type,
            text=text,
            content=content,
        )
        message_id = ""
        if isinstance(payload, dict):
            message_id = payload.get("data", {}).get("message_id", "")
        return ToolServiceResult(
            message=f"已发送飞书消息给 {receive_id}（{receive_id_type}）。message_id={message_id}",
            outputs=[],
            data=to_jsonable(payload),
        )

    # ---- escape hatch (read-only) ------------------------------------------
    def api_get(self, *, path: str, params: Optional[Dict[str, Any]] = None) -> ToolServiceResult:
        path = (path or "").strip()
        if not path.startswith("/"):
            raise ValueError("path 必须以 / 开头，例如 /im/v1/chats")
        payload = run_api("GET", path, params=params or None)
        preview = _clip(json.dumps(to_jsonable(payload), ensure_ascii=False), 1800)
        return ToolServiceResult(
            message=f"GET {path} 返回：\n{preview}",
            outputs=[],
            data=to_jsonable(payload),
        )


__all__ = ["FeishuService"]
