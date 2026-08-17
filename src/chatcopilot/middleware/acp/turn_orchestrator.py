"""Concrete ordered handlers for one ACP prompt turn."""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from acp import PromptResponse

from chatcopilot.middleware.acp import access_gate as _access_gate
from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.core.image_content import ImageContentError, is_supported_image_path
from chatcopilot.middleware.acp import attachment_pipeline as _attachment
from chatcopilot.middleware.acp import attachment_turns as _attachment_turns
from chatcopilot.middleware.acp import deterministic_replies as _deterministic_replies
from chatcopilot.middleware.acp import image_pipeline as _image
from chatcopilot.middleware.acp.prompt_pipeline import build_topic_metadata
from chatcopilot.middleware.acp.turn_pipeline import (
    CallbackTurnHandler,
    OrderedTurnPipeline,
    TurnContext,
    TurnOutcome,
)


_LOGGER = logging.getLogger("chatcopilot.middleware.acp.turn_orchestrator")


class AcpTurnOrchestrator:
    """Own all six ACP stages while delegating transport operations to the host."""

    def __init__(
        self,
        host: Any,
        *,
        platform_type: str,
        has_image_inputs: bool,
        has_role_matrix: bool,
        has_user_files_pipeline: bool,
        has_private_space_inventory: bool,
        update_text: Callable[[str], Any],
        recover_workspace: Callable[..., Any],
        refresh_system_prompt: Callable[[Any], None],
    ) -> None:
        self._host = host
        self._platform_type = platform_type
        self._has_image_inputs = has_image_inputs
        self._has_role_matrix = has_role_matrix
        self._has_user_files_pipeline = has_user_files_pipeline
        self._has_private_space_inventory = has_private_space_inventory
        self._update_text = update_text
        self._recover_workspace = recover_workspace
        self._refresh_system_prompt = refresh_system_prompt

    async def run(
        self,
        *,
        prompt: Sequence[Any],
        session: Any,
        session_id: str,
        message_id: str | None,
    ) -> PromptResponse:
        context = TurnContext(
            session_id=session_id,
            session=session,
            user_text="",
            message_id=message_id,
            metadata={"raw_prompt": prompt},
        )
        pipeline = OrderedTurnPipeline(
            (
                CallbackTurnHandler("attachments", self._attachments),
                CallbackTurnHandler("permissions", self._permissions),
                CallbackTurnHandler(
                    "deterministic_shortcuts", self._deterministic_shortcuts
                ),
                CallbackTurnHandler(
                    "session_materialization", self._session_materialization
                ),
                CallbackTurnHandler("execution", self._execution),
                CallbackTurnHandler("finish", self._finish),
            )
        )
        outcome = await pipeline.run(context)
        if outcome.response is None:
            raise RuntimeError("ACP turn pipeline completed without a response")
        return outcome.response

    async def _attachments(self, turn: TurnContext) -> TurnOutcome:
        prompt_parts = _attachment.normalize_cc_connect_wrapper(
            _attachment.extract_prompt_parts(turn.metadata["raw_prompt"])
        )
        turn.metadata["prompt_parts"] = prompt_parts
        turn.user_text = prompt_parts.text or ""
        turn.turn_task = self._host._start_turn_task(
            session=turn.session,
            session_id=turn.session_id,
            message_id=turn.message_id,
            user_text=turn.user_text,
        )
        _LOGGER.info(
            "session/prompt | sid=%s platform=%s msgs=%d user_text_len=%d "
            "resources=%d mode=%s debug=%s",
            turn.session_id,
            self._platform_type,
            turn.session.message_count(),
            len(turn.user_text),
            len(prompt_parts.resource_names),
            turn.session.assistant_mode.value,
            turn.session.debug_mode,
        )
        return TurnOutcome()

    async def _permissions(self, turn: TurnContext) -> TurnOutcome:
        runtime = getattr(self._host, "_runtime", None)
        access = getattr(runtime, "access", None)
        if access is None or not access.enabled:
            return TurnOutcome()
        mention_name = None
        spec = getattr(runtime, "spec", None)
        platform_spec = getattr(spec, "platform", None)
        if platform_spec is not None:
            mention_name = getattr(platform_spec, "mention_name", None)
        decision = _access_gate.evaluate(
            access,
            platform_type=self._platform_type,
            chat_kind=turn.session.workspace.chat_kind,
            chat_id=turn.session.workspace.chat_id,
            user_id=turn.session.workspace.user_id,
            text=turn.user_text,
            mention_name=mention_name,
        )
        _LOGGER.info(
            "session/prompt | sid=%s access-gate %s | kind=%s uid=%s "
            "reason=%s text=%r",
            turn.session_id,
            "passed" if decision.allowed else "IGNORED",
            turn.session.workspace.chat_kind,
            turn.session.workspace.user_id,
            decision.reason,
            turn.user_text[:120],
        )
        if decision.allowed:
            return TurnOutcome()
        self._host._finish_turn_task(
            turn.turn_task,
            status="succeeded",
            progress="已按访问策略忽略该消息。",
        )
        return TurnOutcome(
            response=PromptResponse(
                stop_reason="end_turn", user_message_id=turn.message_id
            ),
            stop=True,
            reason="access_denied",
        )

    async def _deterministic_shortcuts(self, turn: TurnContext) -> TurnOutcome:
        response = await _deterministic_replies.handle_deterministic_replies(
            conn=self._host._conn,
            session=turn.session,
            session_id=turn.session_id,
            user_text=turn.user_text,
            message_id=turn.message_id,
            turn_task=turn.turn_task,
            has_role_matrix=self._has_role_matrix,
            has_user_files_pipeline=self._has_user_files_pipeline,
            has_private_space_inventory=self._has_private_space_inventory,
            pending_attachment_names=getattr(
                self._host, "_attachment_ack_resource_names", {}
            ).get(turn.session_id, []),
            send_task_status=self._host._send_task_status,
            send_job_status=self._host._send_job_status,
            send_unnotified_completed_jobs=(
                self._host._send_unnotified_completed_jobs
            ),
            handle_code_task_control=self._host._handle_code_task_control,
            cancel_attachment_ack=self._host._cancel_attachment_ack,
            finish_turn_task=self._host._finish_turn_task,
            make_text_update=self._update_text,
        )
        if response is None:
            return TurnOutcome()
        return TurnOutcome(
            response=response,
            stop=True,
            reason="deterministic_shortcut",
        )

    async def _session_materialization(self, turn: TurnContext) -> TurnOutcome:
        prompt_parts = turn.metadata["prompt_parts"]
        referenced = (
            _attachment.collect_attachment_references(
                prompt_parts, turn.user_text
            )
            if self._has_user_files_pipeline
            else []
        )
        imported = (
            _attachment.import_transport_attachments(
                turn.session.workspace, referenced
            )
            if referenced
            else []
        )
        available = [
            name
            for name in referenced
            if name and (turn.session.workspace.attachments / name).is_file()
        ]
        turn.metadata["attachment_import_summary"] = {
            "requested": referenced,
            "imported": imported,
            "available": available,
        }
        turn.metadata["task_resources"] = ()

        raw_prompt = turn.metadata["raw_prompt"]
        inline_images = _image.has_inline_images(raw_prompt)
        referenced_image_names = [
            name for name in referenced if is_supported_image_path(name)
        ]
        has_new_images = inline_images or bool(referenced_image_names)
        if inline_images and not self._has_image_inputs:
            return await self._finish_image_turn(
                turn,
                text=(
                    "当前机器人未启用读图能力。图片不会交给模型分析；"
                    "请让维护者在 BotSpec 中显式启用 chat.image_inputs。"
                ),
                reason="image_inputs_disabled",
                progress="已说明读图能力未启用。",
            )
        if (
            referenced_image_names
            and turn.user_text.strip()
            and not self._has_image_inputs
        ):
            return await self._finish_image_turn(
                turn,
                text=(
                    "当前机器人未启用读图能力。这些图片仍可按普通文件保存，"
                    "但不会交给模型分析。"
                ),
                reason="image_inputs_disabled",
                progress="已说明读图能力未启用。",
            )
        if (
            has_new_images
            and self._has_image_inputs
            and not turn.session.workspace.user_id
        ):
            return await self._finish_image_turn(
                turn,
                text=(
                    "当前会话没有稳定用户身份，不能安全保存或分析图片。"
                    "请重启机器人后重试。"
                ),
                reason="image_identity_missing",
                progress="已拒绝无稳定身份的图片输入。",
            )
        if self._has_image_inputs:
            image_outcome = await self._prepare_image_resources(
                turn,
                referenced_image_names=referenced_image_names,
                has_new_images=has_new_images,
            )
            if image_outcome is not None:
                return image_outcome

        if (
            not turn.metadata["task_resources"]
            and _attachment_turns.is_upload_only_prompt(
                has_user_files_pipeline=self._has_user_files_pipeline,
                prompt_parts=prompt_parts,
                user_text=turn.user_text,
            )
        ):
            attachment_result = await _attachment_turns.handle_upload_only_turn(
                conn=self._host._conn,
                session=turn.session,
                session_id=turn.session_id,
                user_text=turn.user_text,
                message_id=turn.message_id,
                prompt_parts=prompt_parts,
                turn_task=turn.turn_task,
                recover_workspace=self._recover_workspace,
                build_session=self._host._build_session,
                store_session=self._host._store_session,
                cancel_attachment_ack=self._host._cancel_attachment_ack,
                finish_turn_task=self._host._finish_turn_task,
                make_text_update=self._update_text,
            )
            turn.session = attachment_result.session
            return TurnOutcome(
                response=attachment_result.response,
                stop=True,
                reason="attachment_upload",
            )

        if (
            referenced
            and _attachment.has_task_verb(turn.user_text)
            and len(available) < len(referenced)
        ):
            return await self._attachments_pending(
                turn, prompt_parts, referenced, imported, available
            )

        if referenced:
            _LOGGER.info(
                "session/prompt | sid=%s attachments ready for LLM turn | "
                "requested=%d available=%d imported=%d source=%s",
                turn.session_id,
                len(referenced),
                len(available),
                len(imported),
                "acp_resource" if prompt_parts.has_resource else "textified",
            )
        if turn.metadata["task_resources"] or _attachment.has_task_verb(
            turn.user_text
        ):
            self._host._cancel_attachment_ack(turn.session_id)
        hint_names = (
            [name for name in prompt_parts.resource_names if name]
            if prompt_parts.has_resource
            else referenced
        )
        if hint_names:
            hint = "\n".join(f"[资源引用: {name}]" for name in hint_names)
            turn.user_text = f"{turn.user_text}\n{hint}".strip()
        turn.metadata["has_attachment"] = bool(
            hint_names
            or referenced
            or prompt_parts.has_resource
            or turn.metadata["task_resources"]
        )
        turn.session = await self._host._ensure_agent_session(
            turn.session_id, turn.session
        )
        self._refresh_system_prompt(turn.session)
        turn.metadata["task_metadata"] = build_topic_metadata(
            user_text=turn.user_text,
            chat_kind=turn.session.workspace.chat_kind,
            has_attachment=bool(turn.metadata["has_attachment"]),
            message_count=turn.session.message_count(),
        )
        turn.metadata["task_metadata"]["has_image"] = bool(
            turn.metadata["task_resources"]
        )
        return TurnOutcome()

    async def _prepare_image_resources(
        self,
        turn: TurnContext,
        *,
        referenced_image_names: list[str],
        has_new_images: bool,
    ) -> TurnOutcome | None:
        try:
            inline_resources = _image.materialize_inline_images(
                turn.metadata["raw_prompt"],
                turn.session.workspace,
            )
            pending_names = _attachment.dedupe_resource_names(
                [
                    *turn.session.pending_image_names,
                    *referenced_image_names,
                ]
            )
            if pending_names:
                _attachment.import_transport_attachments(
                    turn.session.workspace,
                    pending_names,
                )
            available_names = [
                name
                for name in pending_names
                if (turn.session.workspace.attachments / name).is_file()
            ]
            available_name_set = set(available_names)
            unresolved_names = [
                name for name in pending_names if name not in available_name_set
            ]
            file_resources = tuple(
                _image.image_resource_ref(
                    turn.session.workspace.attachments / name,
                    turn.session.workspace,
                )
                for name in available_names
            )
            resources = _merge_image_resources(
                turn.session.pending_image_resources,
                inline_resources,
                file_resources,
            )
            if (
                len(resources) + len(unresolved_names)
                > _image.DEFAULT_IMAGE_INPUT_MAX_COUNT
            ):
                raise ImageContentError(
                    "too many pending images for one model turn"
                )
        except ImageContentError as exc:
            _LOGGER.warning(
                "session/prompt | sid=%s rejected image input | reason=%s",
                turn.session_id,
                exc,
            )
            return await self._finish_image_turn(
                turn,
                text=(
                    "图片无法安全读取。请发送 JPEG、PNG、GIF 或 WebP；"
                    "单张不超过 5 MiB，每次最多 4 张、合计不超过 20 MiB。"
                ),
                reason="image_input_rejected",
                progress="已拒绝无效或超限的图片输入。",
            )

        if unresolved_names:
            turn.session.pending_image_resources = resources
            turn.session.pending_image_names = tuple(unresolved_names)
            if has_new_images or turn.user_text.strip():
                self._host._schedule_attachment_ack(
                    session_id=turn.session_id,
                    ws=turn.session.workspace,
                    resource_names=unresolved_names,
                )
                return await self._finish_image_turn(
                    turn,
                    text=(
                        "图片正在保存，暂时还不能安全读取。文件可用后，请再发送一次读图指令；"
                        "我不会在没有明确指令时自动分析。"
                    ),
                    reason="images_pending",
                    progress="已回执图片保存中。",
                )
            return None

        if not has_new_images and not turn.user_text.strip():
            return None
        if not resources:
            return None

        turn.session.pending_image_resources = resources
        turn.session.pending_image_names = ()
        if not turn.user_text.strip():
            return await self._finish_image_turn(
                turn,
                text=(
                    f"已收到并安全保存 {len(resources)} 张图片。请发送你希望我执行的读图指令；"
                    "下一条普通消息会使用这些图片一次。"
                ),
                reason="images_staged",
                progress="已保存图片并等待读图指令。",
            )

        turn.metadata["task_resources"] = resources
        return None

    async def _finish_image_turn(
        self,
        turn: TurnContext,
        *,
        text: str,
        reason: str,
        progress: str,
    ) -> TurnOutcome:
        await self._host._conn.session_update(
            session_id=turn.session_id,
            update=self._update_text(text),
        )
        turn.session.record_exchange(
            turn.user_text.strip() or "[图片上传]",
            text,
        )
        self._host._finish_turn_task(
            turn.turn_task,
            progress=progress,
            final_text=text,
            stop_reason="end_turn",
        )
        return TurnOutcome(
            response=PromptResponse(stop_reason="end_turn"),
            stop=True,
            reason=reason,
        )

    async def _attachments_pending(
        self,
        turn: TurnContext,
        prompt_parts: Any,
        referenced: list[str],
        imported: list[str],
        available: list[str],
    ) -> TurnOutcome:
        self._host._schedule_attachment_ack(
            session_id=turn.session_id,
            ws=turn.session.workspace,
            resource_names=referenced,
        )
        text = _attachment.format_attachment_deferred_receipt(referenced)
        await self._host._conn.session_update(
            session_id=turn.session_id,
            update=self._update_text(text),
        )
        _LOGGER.info(
            "session/prompt | sid=%s task deferred until attachments visible | "
            "requested=%d available=%d imported=%d source=%s",
            turn.session_id,
            len(referenced),
            len(available),
            len(imported),
            "acp_resource" if prompt_parts.has_resource else "textified",
        )
        self._host._finish_turn_task(
            turn.turn_task,
            progress="已回复附件保存中，等待文件可用。",
            final_text=text,
            stop_reason="end_turn",
        )
        return TurnOutcome(
            response=PromptResponse(
                stop_reason="end_turn", user_message_id=turn.message_id
            ),
            stop=True,
            reason="attachments_pending",
        )

    async def _execution(self, turn: TurnContext) -> TurnOutcome:
        task_resources = tuple(turn.metadata.get("task_resources") or ())
        if task_resources:
            turn.session.pending_image_resources = ()
            turn.session.pending_image_names = ()
        run_kwargs: dict[str, Any] = {
            "task_metadata": turn.metadata["task_metadata"],
        }
        if task_resources:
            run_kwargs["task_resources"] = task_resources
        turn.metadata["response"] = await self._host._run_agent_turn(
            turn.session,
            turn.session_id,
            turn.user_text,
            turn.message_id,
            turn.turn_task,
            **run_kwargs,
        )
        return TurnOutcome()

    async def _finish(self, turn: TurnContext) -> TurnOutcome:
        return TurnOutcome(
            response=turn.metadata["response"],
            stop=True,
            reason="completed",
        )


def _merge_image_resources(
    *groups: tuple[ResourceRef, ...],
) -> tuple[ResourceRef, ...]:
    merged: list[ResourceRef] = []
    seen: set[str] = set()
    total_bytes = 0
    for group in groups:
        for resource in group:
            sha256 = (resource.sha256 or "").lower()
            if len(sha256) != 64 or any(
                character not in "0123456789abcdef" for character in sha256
            ):
                raise ImageContentError("image resource sha256 identity is missing")
            size_bytes = resource.size_bytes
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes <= 0
            ):
                raise ImageContentError("image resource size identity is missing")
            key = sha256
            if key in seen:
                continue
            seen.add(key)
            merged.append(resource)
            total_bytes += size_bytes
            if len(merged) > _image.DEFAULT_IMAGE_INPUT_MAX_COUNT:
                raise ImageContentError("too many images for one model turn")
            if total_bytes > _image.DEFAULT_IMAGE_INPUT_MAX_TOTAL_BYTES:
                raise ImageContentError("total image bytes exceed turn limit")
    return tuple(merged)


__all__ = ["AcpTurnOrchestrator"]
