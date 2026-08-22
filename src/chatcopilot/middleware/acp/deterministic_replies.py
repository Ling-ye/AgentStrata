"""Deterministic ACP prompt shortcuts before the LLM turn."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from acp import PromptResponse, update_agent_message_text
from acp.interfaces import Client

from chatcopilot.middleware.acp import attachment_pipeline as _attachment
from chatcopilot.middleware.acp import meta_commands as _meta
from chatcopilot.middleware.acp import model_commands as _model_commands
from chatcopilot.middleware.acp import private_space as _private
from chatcopilot.middleware.acp import project_access as _project_access
from chatcopilot.middleware.acp.job_dispatch import (
    extract_code_task_command,
    extract_job_status_query,
)
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.acp.task_dispatch import extract_task_status_query
from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder
from chatcopilot.contracts.identity import Role, role_value
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.deterministic_replies")


FinishTurnTask = Callable[..., None]
SendTaskStatus = Callable[[str, SessionState, str], Awaitable[str]]
SendJobStatus = Callable[[str, SessionState, str], Awaitable[Any]]
SendUnnotifiedJobs = Callable[[str, SessionState], Awaitable[Any]]
HandleCodeTaskControl = Callable[[str, SessionState, str, str], Awaitable[str]]
CancelAttachmentAck = Callable[[str], None]
MakeTextUpdate = Callable[[str], Any]
RefreshPromptPlan = Callable[[SessionState], None]


async def handle_deterministic_replies(
    *,
    conn: Client,
    session: SessionState,
    session_id: str,
    user_text: str,
    message_id: str | None,
    turn_task: TurnTaskRecorder | None,
    has_role_matrix: bool,
    has_user_files_pipeline: bool,
    has_private_space_inventory: bool,
    pending_attachment_names: list[str],
    send_task_status: SendTaskStatus,
    send_job_status: SendJobStatus,
    send_unnotified_completed_jobs: SendUnnotifiedJobs,
    handle_code_task_control: HandleCodeTaskControl,
    cancel_attachment_ack: CancelAttachmentAck,
    finish_turn_task: FinishTurnTask,
    refresh_prompt_plan: RefreshPromptPlan | None = None,
    make_text_update: MakeTextUpdate = update_agent_message_text,
) -> PromptResponse | None:
    shared_group = getattr(session.workspace, "scope", "actor") == WORKSPACE_SCOPE_GROUP_SHARED
    owner_turn = role_value(getattr(session, "role", Role.USER)) == Role.OWNER.value
    runtime_info_reply = _meta._handle_owner_runtime_info_query(session, user_text)
    if runtime_info_reply is not None:
        _LOGGER.info(
            "session/prompt | sid=%s deterministic owner runtime info | user_text=%r",
            session_id,
            user_text,
        )
        await _send_text(conn, session_id, runtime_info_reply, make_text_update)
        finish_turn_task(
            turn_task,
            progress="已完成 Owner 运行时信息查询。",
            final_text=runtime_info_reply,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    restricted_reply = _project_access.restricted_project_request_reply(session, user_text)
    if restricted_reply is not None:
        _LOGGER.info(
            "session/prompt | sid=%s deterministic project access denied",
            session_id,
        )
        await _send_text(conn, session_id, restricted_reply, make_text_update)
        session.record_exchange(user_text, restricted_reply)
        finish_turn_task(
            turn_task,
            progress="已按 Owner-only 项目权限拒绝请求。",
            final_text=restricted_reply,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    code_task_command = extract_code_task_command(user_text)
    if code_task_command is not None:
        if shared_group and not owner_turn:
            text = "代码任务管理仅限 Owner；群号加白不会授予任务控制权限。"
            await _send_text(conn, session_id, text, make_text_update)
            session.record_exchange(user_text, text)
            finish_turn_task(
                turn_task,
                progress="已拒绝群聊代码任务控制。",
                final_text=text,
                stop_reason="end_turn",
            )
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)
        action, task_id = code_task_command
        text = await handle_code_task_control(
            session_id,
            session,
            action,
            task_id,
        )
        finish_turn_task(
            turn_task,
            progress=f"已完成代码任务{action}操作。",
            final_text=text,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    if has_user_files_pipeline and _attachment.is_feishu_file_size_limit_error(user_text):
        text = _attachment.format_feishu_file_size_limit_reply()
        await _send_text(conn, session_id, text, make_text_update)
        session.record_exchange(user_text or "[文件上传失败：文件过大]", text)
        cancel_attachment_ack(session_id)
        _LOGGER.info(
            "session/prompt | sid=%s deterministic feishu file size limit reply",
            session_id,
        )
        finish_turn_task(
            turn_task,
            progress="已回复文件过大提示。",
            final_text=text,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    task_id = extract_task_status_query(user_text)
    if task_id:
        if shared_group:
            text = "QQ 群共享会话不保存成员可见的执行诊断，也不提供任务状态查询。"
            await _send_text(conn, session_id, text, make_text_update)
            session.record_exchange(user_text, text)
            finish_turn_task(
                turn_task,
                progress="已拒绝群聊任务状态查询。",
                final_text=text,
                stop_reason="end_turn",
            )
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)
        _LOGGER.info(
            "session/prompt | sid=%s deterministic task status query | task_id=%s",
            session_id,
            task_id,
        )
        text = await send_task_status(session_id, session, task_id)
        finish_turn_task(
            turn_task,
            progress=f"已完成单轮任务状态查询：{task_id}。",
            final_text=text,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    job_id = extract_job_status_query(user_text)
    if job_id:
        if shared_group and not owner_turn:
            text = "QQ 群普通成员不能启动或查询 Owner 后台任务。"
            await _send_text(conn, session_id, text, make_text_update)
            session.record_exchange(user_text, text)
            finish_turn_task(
                turn_task,
                progress="已拒绝群聊后台任务查询。",
                final_text=text,
                stop_reason="end_turn",
            )
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)
        _LOGGER.info(
            "session/prompt | sid=%s deterministic job status query | job_id=%s",
            session_id,
            job_id,
        )
        await send_job_status(session_id, session, job_id)
        finish_turn_task(turn_task, progress=f"已完成后台任务状态查询：{job_id}。")
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    if not shared_group or owner_turn:
        await send_unnotified_completed_jobs(session_id, session)

    if shared_group and not owner_turn and _model_commands._parse_request(user_text) is not None:
        text = "Codex 开发模型查看与切换仅限 Owner；群号加白不会授予该权限。"
        await _send_text(conn, session_id, text, make_text_update)
        session.record_exchange(user_text, text)
        finish_turn_task(
            turn_task,
            progress="已拒绝群聊 Codex 模型命令。",
            final_text=text,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    model_reply = _model_commands.handle_model_command(session, user_text)
    if model_reply is not None:
        _LOGGER.info(
            "session/prompt | sid=%s deterministic model command | text=%r role=%s",
            session_id,
            user_text,
            session.role.value,
        )
        await _send_text(conn, session_id, model_reply, make_text_update)
        session.record_exchange(user_text, model_reply)
        finish_turn_task(
            turn_task,
            progress="已完成 Codex 开发模型设置。",
            final_text=model_reply,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    mode_reply = (
        _meta._handle_assistant_mode_command(
            session,
            user_text,
            refresh_prompt_plan=refresh_prompt_plan,
        )
        if has_role_matrix
        else None
    )
    if mode_reply is not None:
        _LOGGER.info(
            "session/prompt | sid=%s assistant mode command | text=%r role=%s chat_kind=%s -> mode=%s",
            session_id,
            user_text,
            session.role.value,
            session.workspace.chat_kind,
            session.assistant_mode.value,
        )
        await _send_text(conn, session_id, mode_reply, make_text_update)
        finish_turn_task(
            turn_task,
            progress="已完成业务模式切换。",
            final_text=mode_reply,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    debug_reply = _meta._handle_debug_command(session, user_text) if has_role_matrix else None
    if debug_reply is not None:
        _LOGGER.info(
            "session/prompt | sid=%s /debug command | text=%r role=%s -> debug=%s",
            session_id,
            user_text,
            session.role.value,
            session.debug_mode,
        )
        await _send_text(conn, session_id, debug_reply, make_text_update)
        finish_turn_task(
            turn_task,
            progress="已完成调试模式设置。",
            final_text=debug_reply,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    if has_role_matrix and _meta._should_handle_owner_global_workspace_query(session, user_text):
        text = _meta._format_owner_global_workspace_status(session.workspace)
        await _send_text(conn, session_id, text, make_text_update)
        _LOGGER.info(
            "session/prompt | sid=%s deterministic owner workspace status | user_text=%r",
            session_id,
            user_text,
        )
        finish_turn_task(
            turn_task,
            progress="已完成全局工作区状态查询。",
            final_text=text,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    if has_private_space_inventory and _private.is_workspace_inventory_query(user_text):
        pending_names = list(pending_attachment_names or [])
        imported_names = _attachment.import_transport_attachments(session.workspace, pending_names)
        if imported_names:
            pending_names = imported_names
        saved_pending = _attachment.confirmed_transport_attachments(
            session.workspace,
            pending_names,
            imported_names=imported_names,
        )
        if saved_pending:
            ack_text = _attachment.format_attachment_ack(session.workspace, saved_pending)
            await _send_text(conn, session_id, ack_text, make_text_update)
        cancel_attachment_ack(session_id)
        text = _private.format_workspace_inventory(session.workspace)
        await _send_text(conn, session_id, text, make_text_update)
        _LOGGER.info(
            "session/prompt | sid=%s deterministic workspace inventory | "
            "pending=%d imported=%d ack_sent=%d",
            session_id,
            len(pending_names),
            len(imported_names),
            len(saved_pending),
        )
        finish_turn_task(
            turn_task,
            progress="已完成私人空间清单查询。",
            final_text=text,
            stop_reason="end_turn",
        )
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    return None


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


__all__ = ["handle_deterministic_replies"]
