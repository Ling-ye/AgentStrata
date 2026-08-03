"""Turn task status query helpers for ACP deterministic shortcuts."""
from __future__ import annotations

import re
from typing import Optional

from chatcopilot.core.tasks import TASK_ID_RE

_TASK_STATUS_INTENT_RE = re.compile(
    r"完成|处理完|执行完|跑完|结束|状态|结果|成功|失败|原因|诊断|检查|查一下|查询|done|status|result|why",
    re.IGNORECASE,
)


def extract_task_status_query(text: str) -> Optional[str]:
    match = TASK_ID_RE.search(text or "")
    if match is None:
        return None
    if _TASK_STATUS_INTENT_RE.search(text or ""):
        return match.group(1)
    return match.group(1)


__all__ = ["extract_task_status_query"]
