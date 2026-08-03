"""多维表格 Bitable 增删查（``/bitable/v1`` API，bot 身份）。"""
from __future__ import annotations

from typing import Any, Dict

from chatcopilot.external_tools.shared.lark_cli import run_api

from chatcopilot.external_tools.feishu.modules.urls import parse_bitable_url


def _resolve(url: str, table_id: str) -> tuple[str, str]:
    app_token, url_table = parse_bitable_url(url)
    tid = (table_id or "").strip() or url_table
    if not tid:
        raise ValueError("缺少 table_id：请在 URL 中带 ?table=，或显式提供 table_id")
    return app_token, tid


def list_records(
    url: str,
    *,
    table_id: str = "",
    page_size: int = 20,
    view_id: str = "",
    timeout: int = 60,
) -> Dict[str, Any]:
    """列出多维表格记录（分页），返回 OpenAPI 响应（含 data.items）。"""
    app_token, tid = _resolve(url, table_id)
    params: Dict[str, Any] = {"page_size": max(1, min(page_size, 500))}
    if view_id.strip():
        params["view_id"] = view_id.strip()
    return run_api(
        "GET",
        f"/bitable/v1/apps/{app_token}/tables/{tid}/records",
        params=params,
        timeout=timeout,
    )


def add_record(
    url: str,
    *,
    fields: Dict[str, Any],
    table_id: str = "",
    timeout: int = 60,
) -> Dict[str, Any]:
    """新增一条记录。fields 是 {字段名: 值} 字典。"""
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields 必须是非空对象 {字段名: 值}")
    app_token, tid = _resolve(url, table_id)
    return run_api(
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{tid}/records",
        data={"fields": fields},
        timeout=timeout,
    )


def update_record(
    url: str,
    *,
    record_id: str,
    fields: Dict[str, Any],
    table_id: str = "",
    timeout: int = 60,
) -> Dict[str, Any]:
    """更新一条记录的字段。"""
    record_id = (record_id or "").strip()
    if not record_id:
        raise ValueError("record_id 不能为空")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields 必须是非空对象 {字段名: 值}")
    app_token, tid = _resolve(url, table_id)
    return run_api(
        "PUT",
        f"/bitable/v1/apps/{app_token}/tables/{tid}/records/{record_id}",
        data={"fields": fields},
        timeout=timeout,
    )
