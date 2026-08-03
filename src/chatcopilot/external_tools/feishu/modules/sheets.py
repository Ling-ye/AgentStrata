"""电子表格读写（``/sheets/v2`` values API，bot 身份）。"""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.external_tools.shared.lark_cli import run_api

from chatcopilot.external_tools.feishu.modules.urls import parse_sheet_url


def _full_range(sheet_id: str, range_a1: str) -> str:
    range_a1 = (range_a1 or "").strip()
    if "!" in range_a1:
        return range_a1
    if sheet_id:
        return f"{sheet_id}!{range_a1}" if range_a1 else sheet_id
    if not range_a1:
        raise ValueError("缺少范围：请在 URL 中带 ?sheet=，或显式提供 sheet_id / range")
    return range_a1


def read_range(url: str, *, range_a1: str = "", sheet_id: str = "", timeout: int = 60) -> Dict[str, Any]:
    """按范围读取表格数据，返回 OpenAPI 响应（含 data.valueRange.values）。"""
    token, url_sheet = parse_sheet_url(url)
    sid = sheet_id or url_sheet
    full = _full_range(sid, range_a1)
    return run_api("GET", f"/sheets/v2/spreadsheets/{token}/values/{full}", timeout=timeout)


def write_range(
    url: str,
    *,
    values: List[List[Any]],
    range_a1: str = "",
    sheet_id: str = "",
    timeout: int = 120,
) -> Dict[str, Any]:
    """覆盖写入指定范围。"""
    token, url_sheet = parse_sheet_url(url)
    sid = sheet_id or url_sheet
    full = _full_range(sid, range_a1)
    body = {"valueRange": {"range": full, "values": values}}
    return run_api("PUT", f"/sheets/v2/spreadsheets/{token}/values", data=body, timeout=timeout)


def append_rows(
    url: str,
    *,
    values: List[List[Any]],
    range_a1: str = "",
    sheet_id: str = "",
    timeout: int = 120,
) -> Dict[str, Any]:
    """在已有数据末尾追加行。"""
    token, url_sheet = parse_sheet_url(url)
    sid = sheet_id or url_sheet
    full = _full_range(sid, range_a1 or "A1")
    body = {"valueRange": {"range": full, "values": values}}
    return run_api(
        "POST",
        f"/sheets/v2/spreadsheets/{token}/values_append",
        data=body,
        timeout=timeout,
    )
