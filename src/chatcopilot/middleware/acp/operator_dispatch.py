"""Deliver Owner operator commands before attachment and Agent side effects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from acp import PromptResponse, update_agent_message_text
from acp.interfaces import Client

from chatcopilot.contracts.identity import Role, role_value
from chatcopilot.middleware.acp import operator_commands as _operator_commands
from chatcopilot.middleware.acp.instance_control import InstanceControlError
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.operator_dispatch")

FinishTurnTask = Callable[..., None]
MakeTextUpdate = Callable[[str], Any]


def _end_turn_response(message_id: str | None) -> PromptResponse:
    return PromptResponse(  # type: ignore[call-arg]
        stop_reason="end_turn",
        user_message_id=message_id,
    )


async def handle_operator_replies(
    *,
    conn: Client,
    session: SessionState,
    session_id: str,
    user_text: str,
    message_id: str | None,
    turn_task: TurnTaskRecorder | None,
    has_role_matrix: bool,
    instance_control: Any | None,
    finish_turn_task: FinishTurnTask,
    finish_turn_task_strict: FinishTurnTask | None,
    make_text_update: MakeTextUpdate = update_agent_message_text,
) -> PromptResponse | None:
    """Handle a parsed command or pass legacy commands to their existing owner."""

    command = _operator_commands.parse_slash_command(user_text)
    if command is None:
        return None

    owner_turn = role_value(getattr(session, "role", Role.USER)) == Role.OWNER.value
    host_state: dict[str, object] | None = None
    restart_available = instance_control is not None
    if owner_turn and not command.arguments and command.name == "help":
        restart_available = await _restart_available(session, instance_control)
    elif owner_turn and not command.arguments and command.name == "state":
        host_state, restart_available = await _read_instance_state(
            session,
            instance_control,
        )

    decision = _operator_commands.handle_operator_command(
        session,
        user_text,
        host_state=host_state,
        restart_available=restart_available,
        supports_debug=has_role_matrix,
    )
    if decision is None or decision.action == "passthrough":
        return None
    if decision.action == "reply":
        text = decision.text or "指令处理失败。"
        await _send_text(conn, session_id, text, make_text_update)
        finish_turn_task(
            turn_task,
            progress=f"已完成斜杠指令处理（{decision.code or 'reply'}）。",
            final_text=text,
            stop_reason=decision.code or "operator_reply",
        )
        return _end_turn_response(message_id)

    return await _handle_restart_command(
        conn=conn,
        session=session,
        session_id=session_id,
        message_id=message_id,
        turn_task=turn_task,
        instance_control=instance_control,
        finish_turn_task=finish_turn_task,
        finish_turn_task_strict=finish_turn_task_strict,
        make_text_update=make_text_update,
    )


async def _restart_available(
    session: SessionState,
    instance_control: Any | None,
) -> bool:
    if instance_control is None:
        return False
    instance_id = str(getattr(getattr(session, "runtime", None), "instance_id", "") or "")
    try:
        await asyncio.to_thread(instance_control.preflight_restart, instance_id)
    except InstanceControlError as exc:
        _LOGGER.warning("operator restart unavailable | code=%s", exc.code)
        return False
    except Exception:  # noqa: BLE001 - injected ports fail closed
        _LOGGER.exception("operator restart availability check failed")
        return False
    return True


async def _read_instance_state(
    session: SessionState,
    instance_control: Any | None,
) -> tuple[dict[str, object], bool]:
    if instance_control is None:
        return {"status_known": False, "systemd_available": False}, False
    instance_id = str(getattr(getattr(session, "runtime", None), "instance_id", "") or "")
    try:
        status = await asyncio.to_thread(instance_control.status, instance_id)
    except InstanceControlError as exc:
        systemd_available: bool | None = None
        if exc.code in {
            "systemctl_unavailable",
            "systemd_user_environment_unavailable",
        }:
            systemd_available = False
        state: dict[str, object] = {"status_known": False}
        if systemd_available is not None:
            state["systemd_available"] = systemd_available
        _LOGGER.warning("operator instance status unavailable | code=%s", exc.code)
        return state, False
    except Exception:  # noqa: BLE001 - injected ports fail closed like systemd failures
        _LOGGER.exception("operator instance status failed")
        return {"status_known": False}, False

    return (
        {
            "status_known": True,
            "systemd_available": True,
            "registered": status.load_state == "loaded",
            "running": bool(status.running),
            "load_state": status.load_state,
            "active_state": status.active_state,
            "sub_state": status.sub_state,
        },
        bool(status.running),
    )


async def _handle_restart_command(
    *,
    conn: Client,
    session: SessionState,
    session_id: str,
    message_id: str | None,
    turn_task: TurnTaskRecorder | None,
    instance_control: Any | None,
    finish_turn_task: FinishTurnTask,
    finish_turn_task_strict: FinishTurnTask | None,
    make_text_update: MakeTextUpdate,
) -> PromptResponse:
    if instance_control is None or finish_turn_task_strict is None:
        text = "当前运行环境缺少安全重启或任务终态能力，未调度重启。"
        await _send_text(conn, session_id, text, make_text_update)
        finish_turn_task(
            turn_task,
            status="failed",
            progress="安全重启能力不可用。",
            final_text=text,
            stop_reason="restart_unavailable",
            error="restart_unavailable",
        )
        return _end_turn_response(message_id)

    instance_id = str(getattr(getattr(session, "runtime", None), "instance_id", "") or "")
    try:
        status = await asyncio.to_thread(instance_control.preflight_restart, instance_id)
    except InstanceControlError as exc:
        return await _restart_preflight_failure(
            conn=conn,
            session_id=session_id,
            message_id=message_id,
            turn_task=turn_task,
            finish_turn_task=finish_turn_task,
            make_text_update=make_text_update,
            code=exc.code,
        )
    except Exception:  # noqa: BLE001 - injected ports fail closed
        _LOGGER.exception("operator restart preflight failed")
        return await _restart_preflight_failure(
            conn=conn,
            session_id=session_id,
            message_id=message_id,
            turn_task=turn_task,
            finish_turn_task=finish_turn_task,
            make_text_update=make_text_update,
            code="restart_preflight_failed",
        )
    if not bool(status.running):
        return await _restart_preflight_failure(
            conn=conn,
            session_id=session_id,
            message_id=message_id,
            turn_task=turn_task,
            finish_turn_task=finish_turn_task,
            make_text_update=make_text_update,
            code="instance_not_running",
        )

    accepted_text = (
        f"已接受当前机器人实例 {status.instance_id} 的重启请求。"
        "本条回执和任务终态落盘后，系统会安排延迟重启；"
        "workspace、会话日志、memory、persona 与持久化任务不会被清理。"
        "正在运行的进程内回合可能中断。"
    )
    await _send_text(conn, session_id, accepted_text, make_text_update)
    try:
        finish_turn_task_strict(
            turn_task,
            progress="已送达并持久化当前实例重启请求，等待宿主调度。",
            final_text=accepted_text,
            stop_reason="restart_accepted",
            lifecycle={
                "lifecycle_operation": "restart_instance",
                "lifecycle_status": "accepted",
                "lifecycle_target": "current_instance",
                "final_text_delivered": True,
            },
        )
    except Exception:  # noqa: BLE001 - do not schedule without a durable terminal task
        _LOGGER.exception("operator restart task terminal persistence failed")
        finish_turn_task(
            turn_task,
            status="failed",
            progress="重启请求终态持久化失败，未调度重启。",
            stop_reason="restart_task_persistence_failed",
            error="restart_task_persistence_failed",
        )
        await _send_text_best_effort(
            conn,
            session_id,
            "任务终态未能可靠落盘，本次重启未调度。",
            make_text_update,
        )
        return _end_turn_response(message_id)

    try:
        handle = await asyncio.to_thread(instance_control.schedule_restart, instance_id)
    except InstanceControlError as exc:
        _record_restart_event(turn_task, status="failed", code=exc.code)
        await _send_text_best_effort(
            conn,
            session_id,
            f"重启调度未成功（错误代码：{exc.code}）。当前不会尝试其他重启路径。",
            make_text_update,
        )
        return _end_turn_response(message_id)
    except Exception:  # noqa: BLE001 - injected ports fail closed
        _LOGGER.exception("operator restart scheduling failed")
        _record_restart_event(
            turn_task,
            status="failed",
            code="restart_schedule_failed",
        )
        await _send_text_best_effort(
            conn,
            session_id,
            "重启调度未成功。当前不会尝试其他重启路径。",
            make_text_update,
        )
        return _end_turn_response(message_id)

    if not _record_restart_event(
        turn_task,
        status="scheduled",
        code="restart_scheduled",
        delay_seconds=int(handle.delay_seconds),
    ):
        await _attempt_restart_cancellation(instance_control, handle)
        text = (
            "重启调度回执未能持久化；已尝试停止调度，但无法确认当前实例是否仍会重启，"
            "请立即从宿主核验实例状态。"
        )
        await _send_text_best_effort(conn, session_id, text, make_text_update)
        return _end_turn_response(message_id)

    await _send_text_best_effort(
        conn,
        session_id,
        f"当前实例重启已安排，将在约 {handle.delay_seconds} 秒后执行。",
        make_text_update,
    )
    return _end_turn_response(message_id)


async def _restart_preflight_failure(
    *,
    conn: Client,
    session_id: str,
    message_id: str | None,
    turn_task: TurnTaskRecorder | None,
    finish_turn_task: FinishTurnTask,
    make_text_update: MakeTextUpdate,
    code: str,
) -> PromptResponse:
    text = f"当前实例无法通过安全重启预检（错误代码：{code}），未调度重启。"
    await _send_text(conn, session_id, text, make_text_update)
    finish_turn_task(
        turn_task,
        status="failed",
        progress="当前实例未通过安全重启预检。",
        final_text=text,
        stop_reason=code,
        error=code,
    )
    return _end_turn_response(message_id)


def _record_restart_event(
    turn_task: TurnTaskRecorder | None,
    *,
    status: str,
    code: str,
    delay_seconds: int | None = None,
) -> bool:
    if turn_task is None:
        return False
    payload: dict[str, object] = {
        "operation": "restart_instance",
        "target": "current_instance",
        "status": status,
        "code": code,
    }
    if delay_seconds is not None:
        payload["delay_seconds"] = delay_seconds
    try:
        turn_task.record_event("instance_restart", payload)
    except Exception:  # noqa: BLE001 - caller decides whether cancellation is required
        _LOGGER.exception("operator restart event persistence failed")
        return False
    return True


async def _attempt_restart_cancellation(instance_control: Any, handle: Any) -> None:
    try:
        await asyncio.to_thread(instance_control.cancel, handle)
    except InstanceControlError as exc:
        _LOGGER.warning("operator restart cancellation unproven | code=%s", exc.code)
    except Exception:  # noqa: BLE001 - report ambiguity without claiming cancellation
        _LOGGER.exception("operator restart cancellation could not be proven")


async def _send_text(
    conn: Client,
    session_id: str,
    text: str,
    make_text_update: MakeTextUpdate,
) -> None:
    await conn.session_update(
        session_id=session_id,
        update=make_text_update(text),
    )


async def _send_text_best_effort(
    conn: Client,
    session_id: str,
    text: str,
    make_text_update: MakeTextUpdate,
) -> bool:
    try:
        await _send_text(conn, session_id, text, make_text_update)
    except Exception:  # noqa: BLE001 - the accepted reply already established delivery
        _LOGGER.exception("operator follow-up delivery failed | sid=%s", session_id)
        return False
    return True


__all__ = ["handle_operator_replies"]
