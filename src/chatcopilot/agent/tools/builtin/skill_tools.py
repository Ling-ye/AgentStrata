"""Build ``read_bot_skill`` providers from Bot-scoped immutable indexes."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from chatcopilot.agent.skills.index import read_skill_body_from_index
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.external_tools.shared.spec_helpers import require_arg, schema_property
from chatcopilot.contracts.tools import Handler, ToolContext, ToolDef, ToolResult, object_schema


def _read_bot_skill(
    args: Mapping[str, Any],
    entries: Iterable[SkillIndexEntry],
) -> ToolResult:
    skill_id = require_arg(dict(args), "skill_id").strip()
    entry, body = read_skill_body_from_index(entries, skill_id)
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


def _build_skill_tool(handler: Handler) -> ToolDef:
    return ToolDef(
        name="read_bot_skill",
        summary=(
            "按需读取 BotSpec 注册的某个 skill 完整流程文档。"
            "PromptPlan 的可用 Skills 索引列出了 id 与触发条件；"
            "命中触发条件时先调用本工具读取详细规则，再按规则执行；同会话同 skill 只读一次。"
        ),
        input_schema=object_schema(
            {
                "skill_id": schema_property(
                    type="string",
                    description="目标 skill 的 id（必须来自 PromptPlan 的 Skills 索引）。",
                ),
            },
            required=("skill_id",),
        ),
        output_schema=object_schema(
            {
                "skill_id": {"type": "string"},
                "name": {"type": "string"},
                "body": {"type": "string"},
                "body_path": {"type": "string"},
            },
            required=("skill_id", "name", "body", "body_path"),
        ),
        handler=handler,
        aliases=["读取skill", "load_skill"],
        weight="light",
        category="playbooks.reader",
        owner="agent",
        module=__name__,
    )


def build_skill_provider(entries: Iterable[SkillIndexEntry]) -> ToolProvider:
    """Bind ``read_bot_skill`` to one Bot runtime's immutable skill index."""
    index = tuple(entries)

    def _handler(args: Mapping[str, Any], _ctx: ToolContext) -> ToolResult:
        return _read_bot_skill(args, index)

    return ToolProvider(
        id="playbooks",
        packs={"playbooks.reader": (_build_skill_tool(_handler),)},
        module=__name__,
        description="Bot-scoped lazy playbook reader.",
    )


# The catalog imports a provider blueprint for schema validation; runtime assembly
# always replaces it with a Bot-bound provider from ``build_skill_provider``.
TOOL_PROVIDER = build_skill_provider(())

__all__ = [
    "TOOL_PROVIDER",
    "build_skill_provider",
]
