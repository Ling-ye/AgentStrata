"""Tool pack prompt manifests for ``unity.codebase.read`` and ``unity.skills``.

Both packs share project configuration (``projects.yaml``) but are exposed as
independent tool packs so a bot can include / exclude them individually in its
``bot.yaml``.
"""
from __future__ import annotations

from chatcopilot.contracts.tool_packs import ToolPackPrompt


def build_unity_codebase_read_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="unity.codebase.read",
        prompt_fragments=(
            "查询已注册的 Unity 工程代码时，使用 unity_codebase 工具组，"
            "工具签名都接受 project='sample_game' 这种逻辑工程名（默认 sample_game，可省略）。",
            "用户问“XX 内存 / 对象 / 列表是在哪 new 出来的 / 怎么创建的”这类问题时，标准三步链路：\n"
            "1) unity_find_csharp_symbol(symbol='XX', mode='new_expression') 找所有 `new XX(...)` 位置；\n"
            "2) unity_project_read 读 new 位置上下文，识别包裹方法名；\n"
            "3) 用包裹方法名再调 unity_find_csharp_symbol(mode='callers') 反向追调用方，按需递归。\n"
            "找类/方法定义用 mode='definition'；找所有使用用 mode='references'。",
            "通用关键词搜索（不是 C# 符号，比如搜某个错误字符串、Lua 函数名、配置 key）用 unity_project_search；"
            "列文件用 unity_project_glob。",
            "项目包含自定义 Lua 方言时，先读取该项目 ForAI 文档中声明的语言差异。",
        ),
    )


def build_unity_codebase_skills_pack() -> ToolPackPrompt:
    return ToolPackPrompt(
        name="unity.skills",
        prompt_fragments=(
            "当不确定相关代码 / 文档具体路径时，先用 unity_path_book 做第一跳路由：\n"
            "- mode='keyword' 按中英文关键词查 ForAI 文档；\n"
            "- mode='lua_script' 按 Lua 脚本名查文件；\n"
            "- mode='c_sharp_script' 按 C# 脚本名查文件。\n"
            "拿到候选路径后用 unity.codebase.read 工具组（unity_project_read / unity_project_search "
            "/ unity_find_csharp_symbol）深入。这是 Unity 工程任务的推荐起点。",
        ),
    )


TOOL_PACK_PROMPT_BUILDERS = {
    "unity.codebase.read": build_unity_codebase_read_pack,
    "unity.skills": build_unity_codebase_skills_pack,
}


__all__ = [
    "TOOL_PACK_PROMPT_BUILDERS",
    "build_unity_codebase_read_pack",
    "build_unity_codebase_skills_pack",
]
