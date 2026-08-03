"""通用飞书 tool pack prompt 声明。

每个 builder 只返回该 tool pack 在 system prompt 中追加的领域提示片段；
工具列表由 ``agent.tools.registry`` 通过 ``tool_packs.catalog`` 的 ``tool_modules``
直接发现，无需 tool pack prompt 重复描述。
"""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPrompt


def build_docs_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="feishu.document",
        prompt_fragments=(
            "需要新建飞书云文档或向文档追加内容时，使用 feishu_doc_create / feishu_doc_append 工具（应用身份）。",
        ),
    )


def build_sheets_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="feishu.sheet",
        prompt_fragments=(
            "需要读写飞书电子表格时，使用 feishu_sheet_read / feishu_sheet_write / feishu_sheet_append 工具。"
            "前提：目标表格已共享给本应用。",
        ),
    )


def build_bitable_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="feishu.bitable",
        prompt_fragments=(
            "需要查询或写入飞书多维表格（Bitable）时，使用 feishu_bitable_query / feishu_bitable_add / "
            "feishu_bitable_update 工具。",
        ),
    )


def build_wiki_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="feishu.wiki",
        prompt_fragments=(
            "需要检索公司知识库 / 云盘文档时，使用 feishu_wiki_search / feishu_drive_search 工具；"
            "检索可见范围受应用权限与文档共享范围限制。",
        ),
    )


def build_im_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="feishu.messaging",
        prompt_fragments=(
            "需要主动给指定飞书用户/群发送消息时，使用 feishu_im_send 工具，必须显式提供接收者 ID；"
            "该工具受角色限制，仅 owner 可用。",
        ),
    )


TOOL_PACK_PROMPT_BUILDERS = {
    "feishu.document": build_docs_pack,
    "feishu.sheet": build_sheets_pack,
    "feishu.bitable": build_bitable_pack,
    "feishu.wiki": build_wiki_pack,
    "feishu.messaging": build_im_pack,
}


__all__ = [
    "TOOL_PACK_PROMPT_BUILDERS",
    "build_docs_pack",
    "build_sheets_pack",
    "build_bitable_pack",
    "build_wiki_pack",
    "build_im_pack",
]
