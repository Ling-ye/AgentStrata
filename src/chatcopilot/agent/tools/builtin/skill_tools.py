"""read_bot_skill 工具：按 id 加载 BotSpec 注册的 skill body。

注册表本身在 ``agent/skills/index.py``，由 AgentRuntime 启动期注入。
"""
from __future__ import annotations

from typing import Any, Dict, List

from chatcopilot.agent.skills.index import (
    current_skill_index,
    read_skill_body_by_id,
    set_skill_index,
)
from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.external_tools.shared.spec_helpers import require_arg, schema_property
from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult, object_schema


def _handler_read_bot_skill(args: Dict[str, Any], _ctx: ToolContext) -> ToolResult:
    skill_id = require_arg(args, "skill_id").strip()
    entry, body = read_skill_body_by_id(skill_id)
    summary = (
        f"已读取 skill `{entry.id}` 的完整流程（{entry.name}）。"
        f"按其中规则执行后再回复用户，同会话同 skill 不再重复读取。\n\n"
        f"----\n{body}"
    )
    return ToolResult(
        ok=True,
        summary=summary,
        outputs=[str(entry.body_path)],
        data={
            "skill_id": entry.id,
            "name": entry.name,
            "body": body,
            "body_path": str(entry.body_path),
        },
    )


TOOLS: List[ToolDef] = [
    ToolDef(
        name="read_bot_skill",
        summary=(
            "按需读取 BotSpec 注册的某个 skill 完整流程文档。"
            "PromptPlan 的可用 Skills 索引列出了 id 与触发条件；"
            "命中触发条件时先调用本工具读取详细规则，再按规则执行；同会话同 skill 只读一次。"
        ),
        input_schema=object_schema({
            "skill_id": schema_property(
                type="string",
                description="目标 skill 的 id（必须来自 PromptPlan 的 Skills 索引）。",
            ),
        }, required=("skill_id",)),
        output_schema=object_schema(
            {
                "skill_id": {"type": "string"},
                "name": {"type": "string"},
                "body": {"type": "string"},
                "body_path": {"type": "string"},
            },
            required=("skill_id", "name", "body", "body_path"),
        ),
        handler=_handler_read_bot_skill,
        aliases=["读取skill", "load_skill"],
        weight="light",
        category="playbooks.reader",
        owner="agent",
        module=__name__,
    ),
]

TOOL_PROVIDER = static_tool_provider(
    "playbooks",
    packs={"playbooks.reader": tuple(TOOLS)},
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER", "current_skill_index", "set_skill_index"]
