from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from chatcopilot.contracts.tools import ToolContext
from chatcopilot.external_tools.wiki.spec import TOOLS


def _tool(name: str):
    return next(tool for tool in TOOLS if tool.name == name)


def _context(chat_kind: str = "p2p") -> ToolContext:
    return ToolContext(workspace=SimpleNamespace(chat_kind=chat_kind), caller_role="owner")


def test_wiki_tools_are_owner_private() -> None:
    assert {tool.name for tool in TOOLS} == {
        "wiki_upsert_page",
        "wiki_search",
        "wiki_read_page",
        "wiki_list_pages",
    }
    assert all(tool.requires_role == "owner" for tool in TOOLS)
    assert all(tool.metadata.get("private_chat_only") is True for tool in TOOLS)


def test_upsert_and_search_tools_share_canonical_store(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"CHATCOPILOT_WIKI_ROOT": str(tmp_path / "wiki")}):
        upserted = _tool("wiki_upsert_page").handler(
            {
                "title": "部署约束",
                "summary": "记录部署约束。",
                "facts": ["运行副本不能直接作为源码修改目标。"],
                "procedures": [],
                "open_questions": [],
                "tags": ["部署"],
                "source_text": "运行副本不能直接作为源码修改目标。",
                "source_ref": "chat:deploy",
            },
            _context(),
        )
        created = upserted.data
        searched = _tool("wiki_search").handler(
            {"query": "源码修改目标"}, _context()
        )
        hits = searched.data["hits"]

    assert created["action"] == "created"
    assert hits[0]["page_id"] == created["page"]["page_id"]
    assert hits[0]["heading"] == "事实"


def test_tool_handler_rejects_group_chat_even_without_middleware(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"CHATCOPILOT_WIKI_ROOT": str(tmp_path / "wiki")}):
        with pytest.raises(PermissionError, match="owner 私聊"):
            _tool("wiki_list_pages").handler({}, _context("group"))
