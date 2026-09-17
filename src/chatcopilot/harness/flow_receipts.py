"""Pure projection of committed delivery changes into small observation receipts."""
from __future__ import annotations

from typing import Any


def close_unfinished_steps(task: dict[str, Any], now: float) -> None:
    """Keep a later resume from reviving observations from a stopped execution."""
    for step in task.get("flow_steps", []):
        if step["status"] == "running" and step["parent_id"] != "delivery":
            step.update(status="cancelled" if task["status"] == "cancelled" else "interrupted",
                        interrupted_at=now, conclusion="任务已停止；本步骤未记录可靠终态")


def record_delivery(previous: dict[str, Any], current: dict[str, Any], now: float) -> None:
    if not current.get("flow_version") or current.get("source", {}).get("kind") == "code_health":
        return
    steps = list(current.get("flow_steps", []))
    delivery = current.get("delivery") or {}
    for section, field, title in (("delivery", "commit_sha", "提交候选"), ("delivery", "pr_url", "创建 PR"),
            ("delivery", "checks", "CI 检查"), ("delivery", "merge_sha", "合并结果"),
            ("delivery", "state", "交付状态"), ("cleanup", "", "归档与清理"), ("local_commit", "", "本地提交与回归收录")):
        before, after = previous.get(section), current.get(section)
        if field:
            before, after = (before or {}).get(field), (after or {}).get(field)
        if not after or before == after:
            continue
        if section == "delivery" and field == "state" and after == "pending":
            continue
        locator = {"section": section, **({"field": field} if field else {})}
        status = "recorded"
        conclusion = "交付回执已记录；详见各项实际结果"
        if field == "state":
            labels = {"committed": "已提交任务分支", "pushed": "已推送", "pr_open": "PR 已创建",
                      "waiting_checks": "等待 CI 与自动合并", "checks_failed": "CI 未通过", "updating": "同步主干并复验",
                      "retryable": "交付等待重试", "blocked": "交付受阻", "merged": "已合并到远端 main",
                      "closed": "PR 已关闭", "cancelled": "交付已取消", "cancel_pending": "正在停止交付", "paused": "自动合并已暂停"}
            conclusion = labels.get(after, str(after))
        elif isinstance(after, str):
            conclusion = after
        steps.append({"id": f"receipt-{len(steps)}", "phase": field or section, "title": title,
            "parent_id": "delivery", "status": status, "started_at": None, "finished_at": now,
            "input": {"repository": delivery.get("repository"), "base_branch": delivery.get("base_branch"),
                      "verified_digest": current.get("verified_digest")},
            "conclusion": conclusion, "locator": locator, "receipt": after})
    current["flow_steps"] = steps
