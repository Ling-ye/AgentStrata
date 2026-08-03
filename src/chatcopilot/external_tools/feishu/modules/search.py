"""知识库 / 云盘检索（bot 身份）。

注意：飞书的全局检索在 bot（应用）身份下的可见范围受应用权限与文档共享范围限制；
若返回权限错误，应把真实错误透传给用户，提示在开发者后台开通检索相关权限并把目标
文档/知识库共享给应用。
"""
from __future__ import annotations

from typing import Any, Dict

from chatcopilot.external_tools.shared.lark_cli import run_api


def wiki_search(*, query: str, space_id: str = "", page_size: int = 20, timeout: int = 60) -> Dict[str, Any]:
    """在知识库节点中检索。"""
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    body: Dict[str, Any] = {"query": query, "page_size": max(1, min(page_size, 50))}
    if space_id.strip():
        body["space_id"] = space_id.strip()
    return run_api("POST", "/wiki/v1/nodes/search", data=body, timeout=timeout)


def drive_search(*, query: str, count: int = 20, timeout: int = 60) -> Dict[str, Any]:
    """在云盘文档中检索（按关键字）。"""
    query = (query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    body = {"search_key": query, "count": max(1, min(count, 50))}
    return run_api("POST", "/suite/docs-api/search/object", data=body, timeout=timeout)
