"""Attachment-only ACP prompt orchestration."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from acp import PromptResponse, update_agent_message_text
from acp.interfaces import Client

from chatcopilot.middleware.acp import attachment_pipeline as _attachment
from chatcopilot.middleware.acp.attachment_pipeline import ExtractedPrompt
from chatcopilot.middleware.acp.session_state import SessionState
from chatcopilot.middleware.runtime.tasks import TurnTaskRecorder
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.attachment_turns")

MakeTextUpdate = Callable[[str], Any]


@dataclass(frozen=True)
class AttachmentTurnResult:
    response: PromptResponse
    session: SessionState


def is_upload_only_prompt(
    *,
    has_user_files_pipeline: bool,
    prompt_parts: ExtractedPrompt,
    user_text: str,
) -> bool:
    return bool(
        has_user_files_pipeline
        and (
            _attachment.should_short_circuit_attachment_only(prompt_parts)
            or _attachment.is_textified_attachment_upload_only(user_text)
        )
    )


async def handle_upload_only_turn(
    *,
    conn: Client,
    session: SessionState,
    session_id: str,
    user_text: str,
    message_id: str | None,
    prompt_parts: ExtractedPrompt,
    turn_task: TurnTaskRecorder | None,
    recover_workspace: Callable[[Workspace, str], Workspace | None],
    build_session: Callable[..., SessionState],
    store_session: Callable[[str, SessionState], None],
    cancel_attachment_ack: Callable[[str], None],
    finish_turn_task: Callable[..., None],
    make_text_update: MakeTextUpdate = update_agent_message_text,
) -> AttachmentTurnResult:
    recovered_ws = recover_workspace(session.workspace, user_text)
    if recovered_ws is not None:
        previous_session = session
        session = build_session(session_id=session_id, ws=recovered_ws)
        session.copy_code_model_state_from(previous_session)
        store_session(session_id, session)

    if not session.workspace.user_id:
        text = (
            "已收到附件，但当前会话没有绑定到稳定身份，无法写入会话空间。\n"
            "请重启机器人服务后重新上传文件；我不会自动分析这个文件。"
        )
        await _send_text(conn, session_id, text, make_text_update)
        session.record_exchange(user_text or "[文件上传]", text)
        _LOGGER.warning(
            "session/prompt | sid=%s attachment-only prompt rejected because workspace has no user identity | workspace=%s",
            session_id,
            session.workspace.root,
        )
        finish_turn_task(
            turn_task,
            progress="已回复附件无法保存原因。",
            final_text=text,
            stop_reason="end_turn",
        )
        return AttachmentTurnResult(
            response=PromptResponse(stop_reason="end_turn", user_message_id=message_id),
            session=session,
        )

    resource_names = (
        prompt_parts.resource_names
        if prompt_parts.has_resource
        else _attachment.extract_attachment_names_from_text(user_text)
    )
    if (
        getattr(session.workspace, "scope", "actor")
        == WORKSPACE_SCOPE_GROUP_SHARED
    ):
        text = _attachment.format_group_attachment_binding_rejection(resource_names)
        await _send_text(conn, session_id, text, make_text_update)
        resource_hint = "\n".join(
            f"[资源引用: {name}]" for name in resource_names if name
        )
        accepted_text = "\n".join(
            part for part in (user_text.strip(), resource_hint) if part
        )
        session.record_exchange(accepted_text or "[文件上传]", text)
        cancel_attachment_ack(session_id)
        finish_turn_task(
            turn_task,
            progress="已拒绝无法绑定到当前群消息的附件。",
            final_text=text,
            stop_reason="end_turn",
        )
        return AttachmentTurnResult(
            response=PromptResponse(
                stop_reason="end_turn",
                user_message_id=message_id,
            ),
            session=session,
        )
    imported_names = _attachment.import_transport_attachments(
        session.workspace,
        resource_names,
    )
    saved_now = _attachment.confirmed_transport_attachments(
        session.workspace,
        resource_names,
        imported_names=imported_names,
    )
    cancel_attachment_ack(session_id)
    if saved_now:
        text = _attachment.format_attachment_ack(session.workspace, saved_now)
        ack_kind = "eager_final"
    else:
        text = _attachment.format_attachment_receipt(
            resource_names,
            session.workspace,
        )
        ack_kind = "fallback_receipt"
    await _send_text(conn, session_id, text, make_text_update)
    session.record_exchange(user_text or "[文件上传]", text)
    _LOGGER.info(
        "session/prompt | sid=%s attachment-only eager ack sent | "
        "kind=%s requested=%d saved=%d",
        session_id,
        ack_kind,
        len(resource_names),
        len(saved_now),
    )
    finish_turn_task(
        turn_task,
        progress="已完成附件保存回执。",
        final_text=text,
        stop_reason="end_turn",
    )
    return AttachmentTurnResult(
        response=PromptResponse(stop_reason="end_turn", user_message_id=message_id),
        session=session,
    )


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


__all__ = ["AttachmentTurnResult", "handle_upload_only_turn", "is_upload_only_prompt"]
