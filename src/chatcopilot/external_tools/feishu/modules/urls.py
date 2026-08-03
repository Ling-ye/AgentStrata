"""飞书资源 URL 解析：从 docx / sheets / base / wiki 链接提取类型与 token。"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import parse_qs, urlparse

_DOC_TYPE_KEYWORDS = {"sheets", "docx", "docs", "wiki", "base", "file"}


def parse_doc_url(url: str) -> Tuple[str, str]:
    """解析飞书文档 URL，返回 (doc_type, token)。

    示例:
        "https://xx.feishu.cn/docx/AbCd" -> ("docx", "AbCd")
        "https://xx.feishu.cn/sheets/XyZ" -> ("sheets", "XyZ")
        "https://xx.feishu.cn/base/Bas3?table=tbl1" -> ("base", "Bas3")
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(path_parts):
        if part in _DOC_TYPE_KEYWORDS and i + 1 < len(path_parts):
            return part, path_parts[i + 1]
    if len(path_parts) >= 2:
        return path_parts[-2], path_parts[-1]
    raise ValueError(f"无法从 URL 中识别文档类型和 token: {url}")


def parse_sheet_url(url: str) -> Tuple[str, str]:
    """解析飞书电子表格 URL，返回 (spreadsheet_token, sheet_id)。sheet_id 可能为空。"""
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.rstrip("/").split("/") if p]
    spreadsheet_token = ""
    for i, part in enumerate(path_parts):
        if part == "sheets" and i + 1 < len(path_parts):
            spreadsheet_token = path_parts[i + 1]
            break
    if not spreadsheet_token:
        raise ValueError(f"无法从 URL 中提取 spreadsheet_token: {url}")
    sheet_id = parse_qs(parsed.query).get("sheet", [""])[0]
    return spreadsheet_token, sheet_id


def parse_bitable_url(url: str) -> Tuple[str, str]:
    """解析飞书多维表格 URL，返回 (app_token, table_id)。table_id 可能为空。

    示例:
        "https://xx.feishu.cn/base/Bas3?table=tblABC&view=vewXYZ"
        -> ("Bas3", "tblABC")
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.rstrip("/").split("/") if p]
    app_token = ""
    for i, part in enumerate(path_parts):
        if part in {"base", "wiki"} and i + 1 < len(path_parts):
            app_token = path_parts[i + 1]
            break
    if not app_token and path_parts:
        app_token = path_parts[-1]
    if not app_token:
        raise ValueError(f"无法从 URL 中提取 bitable app_token: {url}")
    table_id = parse_qs(parsed.query).get("table", [""])[0]
    return app_token, table_id
