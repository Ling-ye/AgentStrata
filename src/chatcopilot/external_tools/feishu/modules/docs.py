"""云文档（docx）创建与追加（bot 身份）。

- 创建：复用 lark-cli ``docs +create`` 快捷命令，直接以 Markdown 建档（富格式）。
- 追加：通过 ``/docx/v1`` blocks children API，把 Markdown 按行追加为文本段落
  （稳定但不渲染富 Markdown；需要富格式时建议新建文档）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from chatcopilot.external_tools.shared.lark_cli import (
    run_api,
    run_lark_cli,
)

from chatcopilot.external_tools.feishu.modules.urls import parse_doc_url


def _extract_create_result(stdout: str) -> Dict[str, str]:
    """从 ``docs +create`` 输出里尽量提取 url / document_id / title。"""
    text = (stdout or "").strip()
    result: Dict[str, str] = {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        url_pattern = r"https://" + r"[^\s\"']+/(?:docx|docs)/[A-Za-z0-9]+"
        url_match = re.search(url_pattern, text)
        if url_match:
            result["url"] = url_match.group(0)
        return result

    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if isinstance(data, dict):
        doc = data.get("document", data)
        if isinstance(doc, dict):
            result["document_id"] = str(doc.get("document_id") or doc.get("token") or "")
            result["title"] = str(doc.get("title") or "")
        result["url"] = str(data.get("url") or data.get("document_url") or "")
    return {k: v for k, v in result.items() if v}


def create_doc(*, title: str, markdown: str = "", timeout: int = 120) -> Dict[str, Any]:
    """新建一篇云文档，标题 + Markdown 正文。返回含 url / document_id 的字典。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("title 不能为空")
    content = f"<title>{title}</title>\n{markdown or ''}"
    result = run_lark_cli(
        ["docs", "+create", "--api-version", "v2", "--doc-format", "markdown", "--content", content],
        timeout=timeout,
    )
    info = _extract_create_result(result.stdout)
    info.setdefault("title", title)
    info["raw_output"] = (result.stdout or "").strip()[:2000]
    return info


def _markdown_to_text_blocks(markdown: str) -> List[Dict[str, Any]]:
    """把 Markdown 文本按非空行拆成 docx 文本块（block_type=2）。"""
    blocks: List[Dict[str, Any]] = []
    for line in (markdown or "").splitlines():
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        blocks.append(
            {
                "block_type": 2,
                "text": {"elements": [{"text_run": {"content": stripped}}]},
            }
        )
    return blocks


def append_markdown(*, url: str, markdown: str, timeout: int = 120) -> Dict[str, Any]:
    """把 Markdown 文本按行追加为文档末尾的文本段落。"""
    doc_type, document_id = parse_doc_url(url)
    if doc_type not in {"docx", "docs"}:
        raise ValueError(f"append 仅支持 docx 文档，当前类型: {doc_type}")
    blocks = _markdown_to_text_blocks(markdown)
    if not blocks:
        raise ValueError("markdown 内容为空，无可追加段落")
    # block_id == document_id 表示追加到文档根。index=-1 追加到末尾。
    return run_api(
        "POST",
        f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
        data={"children": blocks, "index": -1},
        timeout=timeout,
    )
