"""通用飞书能力 CLI 调度（被 cli.py / __main__ 调）。"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from chatcopilot.external_tools.feishu.service import FeishuService


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通用飞书工具（云文档 / 电子表格 / 多维表格 / 检索 / 发消息），以应用(bot)身份运行。",
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=[
            "doc-create", "doc-append",
            "sheet-read", "sheet-write", "sheet-append",
            "bitable-query", "bitable-add", "bitable-update",
            "wiki-search", "drive-search",
            "im-send", "api-get",
        ],
        help="要执行的动作",
    )
    parser.add_argument("--url", default="", help="目标文档/表格/多维表格 URL")
    parser.add_argument("--title", default="", help="文档标题（doc-create）")
    parser.add_argument("--markdown", default="", help="Markdown 正文（doc-create / doc-append）")
    parser.add_argument("--range", dest="range_a1", default="", help="表格范围，如 A1:D10")
    parser.add_argument("--sheet-id", default="", help="表格页签 ID")
    parser.add_argument("--values", default="", help="二维数组 JSON（sheet-write / sheet-append）")
    parser.add_argument("--table-id", default="", help="多维表格 table_id")
    parser.add_argument("--view-id", default="", help="多维表格 view_id（bitable-query）")
    parser.add_argument("--record-id", default="", help="多维表格 record_id（bitable-update）")
    parser.add_argument("--fields", default="", help="字段对象 JSON（bitable-add / bitable-update）")
    parser.add_argument("--page-size", type=int, default=20, help="分页大小")
    parser.add_argument("--query", default="", help="检索关键词（wiki-search / drive-search）")
    parser.add_argument("--space-id", default="", help="知识库 space_id（wiki-search）")
    parser.add_argument("--receive-id", default="", help="消息接收者 ID（im-send）")
    parser.add_argument(
        "--receive-id-type", default="open_id",
        choices=["open_id", "user_id", "union_id", "email", "chat_id"],
        help="接收者 ID 类型（im-send）",
    )
    parser.add_argument("--msg-type", default="text", help="消息类型（im-send）")
    parser.add_argument("--text", default="", help="文本消息内容（im-send）")
    parser.add_argument("--content", default="", help="原始 content JSON（im-send 高级用法）")
    parser.add_argument("--path", default="", help="OpenAPI 路径（api-get）")
    parser.add_argument("--params", default="", help="query 参数 JSON（api-get）")
    return parser


def _loads(raw: str, *, name: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"{name} 不能为空")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 不是合法 JSON: {exc}")


def _ensure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def run_cli(args) -> int:
    _ensure_utf8_console()
    service = FeishuService()
    action = args.action
    try:
        if action == "doc-create":
            result = service.create_doc(title=args.title, markdown=args.markdown)
        elif action == "doc-append":
            result = service.append_doc(url=args.url, markdown=args.markdown)
        elif action == "sheet-read":
            result = service.read_sheet(url=args.url, range_a1=args.range_a1, sheet_id=args.sheet_id)
        elif action == "sheet-write":
            result = service.write_sheet(
                url=args.url, values=_loads(args.values, name="--values"),
                range_a1=args.range_a1, sheet_id=args.sheet_id,
            )
        elif action == "sheet-append":
            result = service.append_sheet(
                url=args.url, values=_loads(args.values, name="--values"),
                range_a1=args.range_a1, sheet_id=args.sheet_id,
            )
        elif action == "bitable-query":
            result = service.bitable_query(
                url=args.url, table_id=args.table_id, page_size=args.page_size, view_id=args.view_id,
            )
        elif action == "bitable-add":
            result = service.bitable_add(
                url=args.url, fields=_loads(args.fields, name="--fields"), table_id=args.table_id,
            )
        elif action == "bitable-update":
            result = service.bitable_update(
                url=args.url, record_id=args.record_id,
                fields=_loads(args.fields, name="--fields"), table_id=args.table_id,
            )
        elif action == "wiki-search":
            result = service.wiki_search(query=args.query, space_id=args.space_id, page_size=args.page_size)
        elif action == "drive-search":
            result = service.drive_search(query=args.query, count=args.page_size)
        elif action == "im-send":
            result = service.send_message(
                receive_id=args.receive_id, receive_id_type=args.receive_id_type,
                msg_type=args.msg_type, text=args.text, content=args.content,
            )
        elif action == "api-get":
            params: Dict[str, Any] | None = None
            if args.params.strip():
                params = _loads(args.params, name="--params")
            result = service.api_get(path=args.path, params=params)
        else:
            raise ValueError(f"不支持的 action: {action}")
        print(result.message)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"CLI 执行失败: {exc}")
        return 1
