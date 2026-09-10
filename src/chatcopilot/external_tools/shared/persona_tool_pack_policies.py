"""Persona capability instructions consumed by the unique PromptPlan builder."""

from chatcopilot.contracts.tool_packs import tool_pack_policies


def build_policy():
    return tool_pack_policies(
        "persona.control",
        "本轮提供 persona_manage 时，Owner 明确要求设置、更新、补充、研究或刷新持续人格应调用该工具，不得用聊天中的草稿代替保存。"
        "当前群默认使用 scope=group；宿主从当前用户正文取得要求。明确清空直接调用 clear，含义或作用域不明确才建立提案。要求搜索时使用 research。"
        "人格写入由宿主状态服务执行，与 Codex 原生文件沙箱或仓库修改无关。"
        "只有 committed=true 的真实回执才能表示已保存；不可在工具可用时无依据声称没有写入能力。",
        applies_to_roles=("owner",),
    )


TOOL_PACK_POLICY_BUILDERS = {"persona.control": build_policy}
