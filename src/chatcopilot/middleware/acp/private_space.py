"""Private workspace inventory capability."""
from __future__ import annotations

import re

from chatcopilot.middleware.runtime.workspace import Workspace

_WORKSPACE_INVENTORY_INTENT_RE = re.compile(
    r"(我的|当前|私人)?\s*(空间|私人空间|工作区|工作目录).{0,12}(有哪些|有什么|内容|文件|清单|列表|列出|查看|看一下)"
    r"|(?:有哪些|有什么|列出|查看|看一下).{0,12}(文件|附件|产物|下载数据|上传文件)"
    r"|(?:文件|附件|产物|下载数据|上传文件).{0,12}(有哪些|有什么|内容|清单|列表)"
)
_WORKSPACE_MUTATION_INTENT_RE = re.compile(r"(清空|删除|移除|重置|处理|分析|对比|diff|生成|发送)")


def is_workspace_inventory_query(text: str) -> bool:
    """Return whether the user is only asking to list private-space files."""
    normalized = (text or "").strip()
    if not normalized:
        return False
    if _WORKSPACE_MUTATION_INTENT_RE.search(normalized):
        return False
    return bool(_WORKSPACE_INVENTORY_INTENT_RE.search(normalized))


def collect_workspace_category_files(
    ws: Workspace,
    *,
    subdir: str,
    limit: int,
) -> tuple[int, list[str]]:
    target = ws.resolve_subdir(subdir)
    if not target.is_dir():
        return 0, []

    entries: list[tuple[float, str]] = []
    for path in target.rglob("*"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(target).as_posix()
        except ValueError:
            continue
        entries.append((stat.st_mtime, f"{subdir}/{rel}"))

    entries.sort(key=lambda item: item[0], reverse=True)
    return len(entries), [name for _mtime, name in entries[:limit]]


def format_workspace_inventory(ws: Workspace, *, per_category_limit: int = 20) -> str:
    """Scan and format the current private workspace without involving the LLM."""
    categories = [
        ("attachments", "附件"),
        ("results", "分析产物"),
        ("downloads", "下载数据"),
        ("uploads", "上传文件"),
    ]

    category_rows: list[tuple[str, int, list[str]]] = []
    total = 0
    for subdir, label in categories:
        count, names = collect_workspace_category_files(
            ws,
            subdir=subdir,
            limit=per_category_limit,
        )
        total += count
        category_rows.append((label, count, names))

    if total == 0:
        lines = ["你的私人空间目前全部为空："]
    else:
        lines = [f"你的私人空间目前共有 {total} 个文件："]

    for label, count, names in category_rows:
        lines.append(f"{label}：{count} 个文件")
        lines.extend(f"- {name}" for name in names)
        if count > len(names):
            lines.append(f"- 还有 {count - len(names)} 个文件未展示")

    return "\n".join(lines)
