"""Tool pack prompt manifest for career intelligence."""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPrompt


def build_career_intelligence_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="career.intelligence",
        prompt_fragments=(
            "面向用户指定公司或岗位的 AI/Agent 后端情报时，先用 career_watchlist_show 读取关注范围，再用 "
            "search_company_ai_jobs 查询官方岗位并保存快照。薪资、待遇和面经必须通过 "
            "career_intel_ingest 连同来源类型、日期、样本属性和证据等级保存；不得把匿名单一样本概括为公司普遍情况。",
            "官方岗位源不可用时，根据工具返回的 fallback_query 调用统一信息研究入口；找到官方岗位详情链接后"
            "必须调用 career_jobs_ingest 写回岗位快照，搜索摘要或社区链接不能作为岗位来源。",
        ),
    )


TOOL_PACK_PROMPT_BUILDERS = {"career.intelligence": build_career_intelligence_pack}

__all__ = ["TOOL_PACK_PROMPT_BUILDERS", "build_career_intelligence_pack"]
