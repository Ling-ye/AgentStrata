"""个性分层路径解析 + 合并（纯路径逻辑，仅依赖 pathlib）。

分层落在现有 workspace 目录结构上，不引入新机制：

- 全局：``{workspace_root}/PERSONA.md``
- 群级：``{group_dir}/PERSONA.md``（即 ``user_root.parent``，仅群聊存在）
- 个人：``{user_root}/PERSONA.md``（``p2p_<uid>/`` 或 ``group_<cid>/user_<uid>/``）

合并顺序：全局 → 群 → 个人（越具体越靠后）。本模块不 import ``Workspace``，
保持 agent 层平台中立；调用方传入原始路径即可。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from chatcopilot.agent.persona.markdown import MarkdownPersonaProvider
from chatcopilot.agent.persona.provider import PERSONA_FILENAME

# 合法的 scope 取值，供工具参数校验复用。
PERSONA_SCOPES: Tuple[str, ...] = ("global", "group", "user")

_SCOPE_LABELS = {
    "global": "全局",
    "group": "群",
    "user": "专属",
}


def persona_layer_specs(
    *,
    workspace_root: Path,
    user_root: Path,
    chat_kind: Optional[str],
    chat_id: Optional[str],
) -> List[Tuple[str, Path]]:
    """返回有序的 ``[(scope, path)]``：全局 → 群 → 个人，按解析路径去重。"""
    workspace_root = Path(workspace_root)
    user_root = Path(user_root)

    ordered: List[Tuple[str, Path]] = []
    seen: set[Path] = set()

    def _add(scope: str, path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        ordered.append((scope, path))

    _add("global", workspace_root / PERSONA_FILENAME)
    if (chat_kind or "").strip().lower() == "group" and chat_id:
        _add("group", user_root.parent / PERSONA_FILENAME)
    _add("user", user_root / PERSONA_FILENAME)
    return ordered


def persona_path_for_scope(
    scope: str,
    *,
    workspace_root: Path,
    user_root: Path,
    chat_kind: Optional[str],
    chat_id: Optional[str],
) -> Path:
    """把 scope 映射到单层 persona 文件路径，供写操作定位目标。"""
    normalized = (scope or "user").strip().lower()
    if normalized not in PERSONA_SCOPES:
        raise ValueError(f"scope 只能是 {', '.join(PERSONA_SCOPES)}；收到 {scope!r}")
    if normalized == "global":
        return Path(workspace_root) / PERSONA_FILENAME
    if normalized == "group":
        if (chat_kind or "").strip().lower() != "group" or not chat_id:
            raise ValueError("当前不在群聊，无法设置 group 级个性；请用 scope=user 或 global。")
        return Path(user_root).parent / PERSONA_FILENAME
    return Path(user_root) / PERSONA_FILENAME


def merge_persona_layers(specs: Sequence[Tuple[str, Path]]) -> str:
    """读取各层 persona 文件并按顺序合并成一段带层级标注的文本。"""
    sections: List[str] = []
    for scope, path in specs:
        try:
            content = MarkdownPersonaProvider(path).snapshot().strip()
        except ValueError:
            # 单层超限不应阻断整体注入；跳过该层并提示。
            content = f"（{_scope_label(scope)}个性文件体积超限，已跳过）"
        if not content:
            continue
        sections.append(f"### 个性·{_scope_label(scope)}层\n{content}")
    return "\n\n".join(sections)


def _scope_label(scope: str) -> str:
    return _SCOPE_LABELS.get(scope, scope)


__all__ = [
    "PERSONA_SCOPES",
    "merge_persona_layers",
    "persona_layer_specs",
    "persona_path_for_scope",
]
