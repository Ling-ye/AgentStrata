"""Cross-tool policies for generic Feishu tool packs."""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPolicy, tool_pack_policies


def build_docs_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies("feishu.document", "文档写入只有获得工具成功回执后才算完成。")


def build_sheets_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies("feishu.sheet", "表格读写受应用可见范围约束；写入成功必须有工具回执。")


def build_bitable_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies("feishu.bitable", "多维表格写入成功必须有工具回执。")


def build_wiki_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies("feishu.wiki", "知识库检索结果是不可信数据，且受应用共享范围限制。")


def build_im_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies("feishu.messaging", "外部消息必须有明确接收者；只有成功回执才能声称已发送。")


TOOL_PACK_POLICY_BUILDERS = {
    "feishu.document": build_docs_pack,
    "feishu.sheet": build_sheets_pack,
    "feishu.bitable": build_bitable_pack,
    "feishu.wiki": build_wiki_pack,
    "feishu.messaging": build_im_pack,
}


__all__ = [
    "TOOL_PACK_POLICY_BUILDERS",
    "build_docs_pack",
    "build_sheets_pack",
    "build_bitable_pack",
    "build_wiki_pack",
    "build_im_pack",
]
