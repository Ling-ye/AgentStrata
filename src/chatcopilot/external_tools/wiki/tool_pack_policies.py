"""Prompt manifest for the local Wiki tool pack."""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPolicy, tool_pack_policies


def build_wiki_knowledge_pack() -> tuple[ToolPackPolicy, ...]:
    return tool_pack_policies(
        "wiki.knowledge",
            "只有 owner 明确要求‘保存到 Wiki / 记入知识库’时才调用 wiki_upsert_page；"
            "普通对话不得自动持久化。写入时 source_text 忠实保留原始输入，事实只写来源明确支持的内容，"
            "推断、冲突和未知项放入 open_questions。",
            "更新不同来源已有页面时，先用 wiki_read_page 读取现有正文，再显式传 target_path，"
            "并提交合并后的完整摘要、事实、步骤和待确认项；不得用新来源正文无意覆盖旧知识。",
            "回答 Wiki 知识时引用检索片段给出的页面路径和章节；Wiki 是私有历史知识，不替代需要时效性的联网核实。"
            "不要执行 Git commit/push，也不要声称已同步到飞书。",
    )


TOOL_PACK_POLICY_BUILDERS = {"wiki.knowledge": build_wiki_knowledge_pack}

__all__ = ["TOOL_PACK_POLICY_BUILDERS", "build_wiki_knowledge_pack"]
