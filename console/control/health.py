from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from console.control import operations, services
from console.control.instances import BotInstance


class TaskSnapshotProvider(Protocol):
    def list(self) -> list[dict[str, object]]:
        ...


Issue = dict[str, Any]

_WORKSPACE_FAILURES_CACHE: tuple[list[dict[str, Any]], float] | None = None
_WORKSPACE_FAILURES_CACHE_TTL = 30.0


def overview(instances: Iterable[BotInstance], task_provider: TaskSnapshotProvider) -> dict[str, Any]:
    bot_statuses = [_bot_status(inst) for inst in instances]
    infra_statuses = [
        _with_infra_checks(item)
        for item in services.all_services_status()
        if item.get("service_type") in ("compose", "standalone")
    ]
    in_memory_tasks = task_provider.list()
    workspace_failures = _recent_workspace_failures(instances)

    issues: list[Issue] = []
    for bot in bot_statuses:
        issues.extend(_bot_issues(bot))
    for item in infra_statuses:
        issues.extend(_infra_issues(item))
    issues.extend(_task_issues(in_memory_tasks, workspace_failures))
    issues.sort(key=_issue_sort_key)

    active_tasks = [task for task in in_memory_tasks if task.get("status") == "running"]
    recent_failures = [
        task for task in in_memory_tasks if task.get("status") == "failed"
    ][:5] + workspace_failures[:5]

    unhealthy_bots = sum(1 for bot in bot_statuses if _bot_health_color(bot) in {"red", "yellow"})
    healthy_infra = sum(1 for item in infra_statuses if item.get("color") == "green")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "bots_total": len(bot_statuses),
            "bots_running": sum(1 for bot in bot_statuses if bot.get("running")),
            "bots_unhealthy": unhealthy_bots,
            "infra_total": len(infra_statuses),
            "infra_healthy": healthy_infra,
            "infra_unhealthy": len(infra_statuses) - healthy_infra,
            "tasks_running": len(active_tasks),
            "tasks_failed_recent": len(recent_failures),
            "issues_critical": sum(1 for issue in issues if issue.get("severity") == "critical"),
            "issues_warning": sum(1 for issue in issues if issue.get("severity") == "warning"),
        },
        "issues": issues,
        "bots": [
            {
                **bot,
                "health_color": _bot_health_color(bot),
                "health_label": _bot_health_label(bot),
            }
            for bot in bot_statuses
        ],
        "infra": infra_statuses,
        "active_tasks": active_tasks,
        "recent_failures": recent_failures[:8],
    }


def _bot_status(inst: BotInstance) -> dict[str, Any]:
    status = operations.status(inst, include_services=False)
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []

    def add(name: str, ok: bool, severity: str, message: str) -> None:
        checks.append({"name": name, "ok": ok, "severity": severity, "message": message})
        if not ok:
            reasons.append(message)

    add("deployed", bool(status.get("is_deployed")), "critical", "Instance files are not deployed.")
    add("registered", bool(status.get("registered")), "critical", "systemd unit is not registered.")
    add("running", bool(status.get("running")), "critical", "bot process is not running.")
    if status.get("running") and status.get("ws_connected") is False:
        add("platform_connection", False, "critical", "platform websocket is not connected.")
    elif status.get("running") and status.get("ws_connected") is True:
        add("platform_connection", True, "info", "platform websocket is connected.")
    cc_age = status.get("cc_log_age_s")
    if status.get("cc_log_size") is not None:
        age_text = f" Last updated {int(float(cc_age))}s ago." if cc_age is not None else ""
        add("fresh_logs", True, "info", f"cc-connect log is available.{age_text}")
    error_count = int(status.get("error_count") or 0)
    if error_count > 0:
        summary = str(status.get("error_summary") or "").strip()
        suffix = f": {summary}" if summary else "."
        add("log_errors", False, "warning", f"cc-connect tail contains {error_count} error line(s){suffix}")
    relay_error_count = int(status.get("qq_relay_error_count") or 0)
    if relay_error_count > 0:
        summary = str(status.get("qq_relay_error_summary") or "").strip()
        suffix = f": {summary}" if summary else "."
        add("qq_relay", False, "critical", f"QQ @ Relay has {relay_error_count} upstream error line(s){suffix}")

    status["checks"] = checks
    status["reasons"] = reasons
    return status


def _with_infra_checks(item: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    color = str(item.get("color") or "grey")
    state = str(item.get("state") or "unknown")

    def add(name: str, ok: bool, severity: str, message: str) -> None:
        checks.append({"name": name, "ok": ok, "severity": severity, "message": message})
        if not ok:
            reasons.append(message)

    add("container", color in {"green", "yellow"}, "critical", f"service state is {state}.")
    if item.get("env_configured") is False:
        add("env", False, "warning", "required environment variable is not configured.")
    if item.get("has_login") and item.get("login_state") == "logged_out":
        add("login", False, "warning", "login state is logged out.")
    item["checks"] = checks
    item["reasons"] = reasons
    return item


def _bot_issues(bot: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for check in bot.get("checks") or []:
        if check.get("ok"):
            continue
        severity = _severity(check.get("severity"))
        issues.append(
            _issue(
                severity=severity,
                source_type="bot",
                source_id=str(bot.get("instance_id") or ""),
                source_name=str(bot.get("display_name") or bot.get("instance_id") or ""),
                title=str(check.get("message") or "bot health check failed"),
                detail=f"{bot.get('active_state') or 'unknown'} / {bot.get('sub_state') or ''}".strip(),
                action_label="Open bot",
                target_page="bots",
                target_id=str(bot.get("instance_id") or ""),
            )
        )
    return issues


def _infra_issues(item: dict[str, Any]) -> list[Issue]:
    issues: list[Issue] = []
    for check in item.get("checks") or []:
        if check.get("ok"):
            continue
        issues.append(
            _issue(
                severity=_severity(check.get("severity")),
                source_type="infra",
                source_id=str(item.get("id") or ""),
                source_name=str(item.get("display_name") or item.get("id") or ""),
                title=str(check.get("message") or "infra health check failed"),
                detail=f"{item.get('service_type') or 'service'} / {item.get('state') or 'unknown'}",
                action_label="Open service",
                target_page="services",
                target_id=str(item.get("id") or ""),
            )
        )
    return issues


def _task_issues(in_memory_tasks: list[dict[str, object]], workspace_failures: list[dict[str, Any]]) -> list[Issue]:
    issues: list[Issue] = []
    for task in in_memory_tasks:
        if task.get("status") == "failed":
            issues.append(
                _issue(
                    severity="warning",
                    source_type="task",
                    source_id=str(task.get("id") or ""),
                    source_name=str(task.get("kind") or "task"),
                    title="Console task failed.",
                    detail=str(task.get("instance_id") or ""),
                    action_label="Open bot",
                    target_page="bots",
                    target_id=str(task.get("instance_id") or ""),
                )
            )
    for task in workspace_failures[:5]:
        issues.append(
            _issue(
                severity="warning",
                source_type="task",
                source_id=str(task.get("task_id") or ""),
                source_name=str(task.get("description") or "agent task"),
                title="Recent agent task failed.",
                detail=str(task.get("progress") or task.get("status") or ""),
                action_label="Open bot",
                target_page="bots",
                target_id=str(task.get("instance_id") or ""),
            )
        )
    return issues


def _recent_workspace_failures(instances: Iterable[BotInstance]) -> list[dict[str, Any]]:
    global _WORKSPACE_FAILURES_CACHE
    now = time.monotonic()
    if _WORKSPACE_FAILURES_CACHE is not None:
        cached, ts = _WORKSPACE_FAILURES_CACHE
        if now - ts < _WORKSPACE_FAILURES_CACHE_TTL:
            return cached

    failures: list[dict[str, Any]] = []
    for inst in instances:
        try:
            response = operations.tasks(inst, limit=20)
        except Exception:  # noqa: BLE001 - overview must stay best-effort.
            continue
        for task in response.get("tasks", []):
            if str(task.get("status") or "").lower() == "failed":
                failures.append({**task, "instance_id": inst.instance_id})
    failures.sort(key=lambda item: float(item.get("sort_time") or 0), reverse=True)
    result = failures[:8]
    _WORKSPACE_FAILURES_CACHE = (result, now)
    return result


def _issue(
    *,
    severity: str,
    source_type: str,
    source_id: str,
    source_name: str,
    title: str,
    detail: str,
    action_label: str,
    target_page: str,
    target_id: str,
) -> Issue:
    return {
        "id": f"{source_type}:{source_id}:{title}",
        "severity": severity,
        "source_type": source_type,
        "source_id": source_id,
        "source_name": source_name,
        "title": title,
        "detail": detail,
        "action_label": action_label,
        "target_page": target_page,
        "target_id": target_id,
        "created_at": time.time(),
    }


def _severity(value: object) -> str:
    return str(value) if value in {"critical", "warning", "info"} else "warning"


def _issue_sort_key(issue: Issue) -> tuple[int, str, str]:
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    return (
        severity_rank.get(str(issue.get("severity")), 3),
        str(issue.get("source_type") or ""),
        str(issue.get("source_name") or ""),
    )


def _bot_health_color(bot: dict[str, Any]) -> str:
    severities = {check.get("severity") for check in bot.get("checks") or [] if not check.get("ok")}
    if "critical" in severities:
        return "red"
    if "warning" in severities:
        return "yellow"
    return "green"


def _bot_health_label(bot: dict[str, Any]) -> str:
    color = _bot_health_color(bot)
    if color == "red":
        return "needs action"
    if color == "yellow":
        return "warning"
    return "healthy"
