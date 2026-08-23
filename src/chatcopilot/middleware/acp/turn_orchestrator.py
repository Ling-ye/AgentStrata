"""Concrete ordered handlers for one ACP prompt turn."""

from __future__ import annotations

import logging
from dataclasses import replace
from collections.abc import Callable, Sequence
from typing import Any, Mapping

from acp import PromptResponse

from chatcopilot.middleware.acp import access_gate as _access_gate
from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.contracts.identity import TurnIdentity
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.core.ingress_receipts import (
    IngressReceiptError,
    consume_ingress_receipt,
    receipt_root_from_env,
)
from chatcopilot.core.image_content import ImageContentError, is_supported_image_path
from chatcopilot.middleware.acp import attachment_pipeline as _attachment
from chatcopilot.middleware.acp import attachment_turns as _attachment_turns
from chatcopilot.middleware.acp import deterministic_replies as _deterministic_replies
from chatcopilot.middleware.acp import image_pipeline as _image
from chatcopilot.middleware.acp.prompt_pipeline import build_topic_metadata
from chatcopilot.middleware.acp.group_conversation import SenderEnvelopeError
from chatcopilot.middleware.acp.turn_pipeline import (
    CallbackTurnHandler,
    OrderedTurnPipeline,
    TurnContext,
    TurnOutcome,
)


_LOGGER = logging.getLogger("chatcopilot.middleware.acp.turn_orchestrator")


class AcpTurnOrchestrator:
    """Own the ordered ACP stages while delegating transport operations to the host."""

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
        refresh_prompt_plan: Callable[[Any], None],
        prepare_turn_identity: Callable[..., tuple[Any, str, TurnIdentity | None]] | None = None,
        activate_turn_identity: Callable[..., Any] | None = None,
    ) -> None:
        self._host = host
        self._platform_type = platform_type
        self._has_image_inputs = has_image_inputs
        self._has_role_matrix = has_role_matrix
        self._has_user_files_pipeline = has_user_files_pipeline
        self._has_private_space_inventory = has_private_space_inventory
        self._update_text = update_text
        self._recover_workspace = recover_workspace
        self._refresh_prompt_plan = refresh_prompt_plan
        self._prepare_turn_identity = prepare_turn_identity or (
            lambda **kwargs: (kwargs["session"], kwargs["user_text"], None)
        )
        self._activate_turn_identity = activate_turn_identity or (
            lambda **kwargs: kwargs["session"]
        )

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
                CallbackTurnHandler("deterministic_shortcuts", self._deterministic_shortcuts),
                CallbackTurnHandler("session_materialization", self._session_materialization),
                CallbackTurnHandler("execution", self._execution),
                CallbackTurnHandler("finish", self._finish),
            )
        )
        try:
            outcome = await pipeline.run(context)
            if outcome.response is None:
                raise RuntimeError("ACP turn pipeline completed without a response")
        except Exception as exc:  # noqa: BLE001 - persist and fail closed at ingress
            _LOGGER.exception(
                "session/prompt | sid=%s inbound pipeline failed",
                session_id,
            )
            if context.turn_task is None:
                tracked = self._start_turn_task(
                    context,
                    user_text="（入站消息内容未保存：处理管线异常）",
                    unauthenticated_intake=True,
                )
                if not tracked:
                    unavailable = await self._tracking_unavailable(context)
                    if unavailable.response is None:
                        raise RuntimeError("tracking failure produced no ACP response")
                    return unavailable.response
            self._record_flow_transition(
                context,
                kind="middleware.pipeline_failed",
                source_layer="middleware",
                target_layer="delivery",
                status="failed",
                title="ACP 入站管线失败",
                summary="失败发生在 Agent 执行前；详细错误按任务脱敏策略保留。",
                decision={"code": "inbound_pipeline_error", "authoritative": True},
            )
            self._host._finish_turn_task(
                context.turn_task,
                status="failed",
                progress="入站消息处理失败。",
                stop_reason="inbound_pipeline_error",
                error=type(exc).__name__,
            )
            await self._host._conn.session_update(
                session_id=session_id,
                update=self._update_text("消息处理失败，请让维护者查看控制台任务记录。"),
            )
            return PromptResponse(
                stop_reason="end_turn",
                user_message_id=message_id,
            )
        return outcome.response

    async def _attachments(self, turn: TurnContext) -> TurnOutcome:
        prompt_parts = _attachment.extract_prompt_parts(turn.metadata["raw_prompt"])
        try:
            turn.session, clean_text, turn_identity = self._prepare_turn_identity(
                session=turn.session,
                session_id=turn.session_id,
                message_id=turn.message_id,
                user_text=prompt_parts.text or "",
            )
        except SenderEnvelopeError as exc:
            return await self._identity_rejection(turn, exc)
        prompt_parts = _attachment.normalize_cc_connect_wrapper(
            replace(prompt_parts, text=clean_text)
        )
        turn.metadata["prompt_parts"] = prompt_parts
        turn.metadata["turn_identity"] = turn_identity
        turn.user_text = prompt_parts.text or ""
        task_workspace = turn.session.workspace
        if turn_identity is not None:
            task_workspace = replace(
                task_workspace,
                user_id=turn_identity.sender_user_id,
                user_name=turn_identity.sender_user_name,
            )
        if not self._start_turn_task(turn, workspace=task_workspace):
            return await self._tracking_unavailable(turn)
        self._record_ingress_receipt(
            turn,
            workspace=task_workspace,
            turn_identity=turn_identity,
        )
        identity_source = (
            str(getattr(turn_identity, "source", "") or "transport_attestation")
            if turn_identity is not None
            else "session_identity"
        )
        self._record_flow_transition(
            turn,
            kind="middleware.identity_validated",
            source_layer="gateway",
            target_layer="middleware",
            status="succeeded",
            title="入站身份已绑定到当前回合",
            summary=(
                "共享会话已通过发送者 envelope 与独立 transport attestation 绑定。"
                if turn_identity is not None
                else "当前平台会话身份已解析；该证据不包含上游网关的具体准入判断。"
            ),
            decision={
                "code": identity_source,
                "allowed": True,
                "authoritative": True,
            },
            payload={
                "adapter": self._platform_type,
                "chat_kind": str(getattr(task_workspace, "chat_kind", "") or ""),
                "message_kind": "resource" if prompt_parts.resource_names else "text",
                "text_length": len(turn.user_text),
            },
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
            self._record_flow_transition(
                turn,
                kind="middleware.access_decision",
                source_layer="middleware",
                target_layer="middleware",
                status="succeeded",
                title="ACP 访问策略允许继续",
                summary="实例未启用额外访问矩阵；身份激活仍服从既有平台契约。",
                decision={
                    "code": "access_policy_disabled",
                    "allowed": True,
                    "authoritative": True,
                },
            )
            return await self._activate_allowed_identity(turn)
        mention_name = None
        spec = getattr(runtime, "spec", None)
        platform_spec = getattr(spec, "platform", None)
        if platform_spec is not None:
            mention_name = getattr(platform_spec, "mention_name", None)
        turn_identity = turn.metadata.get("turn_identity")
        workspace = turn.session.workspace
        chat_kind = (
            turn_identity.conversation.chat_kind
            if turn_identity is not None
            else workspace.chat_kind
        )
        chat_id = (
            turn_identity.conversation.chat_id if turn_identity is not None else workspace.chat_id
        )
        user_id = turn_identity.sender_user_id if turn_identity is not None else workspace.user_id
        decision = _access_gate.evaluate(
            access,
            platform_type=self._platform_type,
            chat_kind=chat_kind,
            chat_id=chat_id,
            user_id=user_id,
            text=turn.user_text,
            mention_name=mention_name,
        )
        _LOGGER.info(
            "session/prompt | sid=%s access-gate %s | kind=%s uid=%s reason=%s text=%r",
            turn.session_id,
            "passed" if decision.allowed else "IGNORED",
            chat_kind,
            user_id,
            decision.reason,
            turn.user_text[:120],
        )
        if decision.allowed:
            self._record_flow_transition(
                turn,
                kind="middleware.access_decision",
                source_layer="middleware",
                target_layer="middleware",
                status="succeeded",
                title="ACP 访问策略允许继续",
                summary="仅记录决定代码，不公开准入名单或稳定平台身份。",
                decision={
                    "code": decision.reason,
                    "allowed": True,
                    "authoritative": True,
                },
                payload={"chat_kind": str(chat_kind or "")},
            )
            return await self._activate_allowed_identity(turn)
        self._record_flow_transition(
            turn,
            kind="middleware.access_decision",
            source_layer="middleware",
            target_layer="delivery",
            status="skipped",
            title="ACP 访问策略忽略消息",
            summary="消息未进入 Agent；准入名单和稳定身份不会写入任务流。",
            decision={
                "code": decision.reason,
                "allowed": False,
                "authoritative": True,
            },
            payload={"chat_kind": str(chat_kind or "")},
        )
        self._host._finish_turn_task(
            turn.turn_task,
            status="succeeded",
            progress="已按访问策略忽略该消息。",
            stop_reason="access_denied",
        )
        return TurnOutcome(
            response=PromptResponse(stop_reason="end_turn", user_message_id=turn.message_id),
            stop=True,
            reason="access_denied",
        )

    async def _activate_allowed_identity(self, turn: TurnContext) -> TurnOutcome:
        try:
            turn.session = self._activate_turn_identity(
                session=turn.session,
                session_id=turn.session_id,
                identity=turn.metadata.get("turn_identity"),
            )
        except SenderEnvelopeError as exc:
            return await self._identity_rejection(turn, exc)
        self._record_flow_transition(
            turn,
            kind="middleware.identity_activated",
            source_layer="middleware",
            target_layer="middleware",
            status="succeeded",
            title="可信调用者身份已激活",
            summary="角色、工作区和能力边界由宿主可信状态解析。",
            decision={"code": "identity_activated", "authoritative": True},
        )
        return TurnOutcome()

    async def _identity_rejection(
        self,
        turn: TurnContext,
        exc: SenderEnvelopeError,
    ) -> TurnOutcome:
        _LOGGER.warning(
            "session/prompt | sid=%s rejected shared-group identity | code=%s",
            turn.session_id,
            exc.code,
        )
        if turn.turn_task is None:
            tracked = self._start_turn_task(
                turn,
                user_text="（入站消息内容未保存：身份校验失败）",
                unauthenticated_intake=True,
            )
            if not tracked:
                return await self._tracking_unavailable(turn)
        self._record_flow_transition(
            turn,
            kind="middleware.identity_rejected",
            source_layer="gateway",
            target_layer="delivery",
            status="failed",
            title="入站身份校验失败",
            summary="任务记录已脱敏，消息正文和未验证发送者未被保存。",
            decision={
                "code": exc.code,
                "allowed": False,
                "authoritative": True,
            },
        )
        self._host._finish_turn_task(
            turn.turn_task,
            status="failed",
            progress="已拒绝缺少可信身份的入站消息。",
            stop_reason=exc.code,
            error=exc.code,
        )
        await self._host._conn.session_update(
            session_id=turn.session_id,
            update=self._update_text(str(exc)),
        )
        return TurnOutcome(
            response=PromptResponse(
                stop_reason="end_turn",
                user_message_id=turn.message_id,
            ),
            stop=True,
            reason=exc.code,
        )

    def _start_turn_task(
        self,
        turn: TurnContext,
        *,
        workspace: Any | None = None,
        user_text: str | None = None,
        unauthenticated_intake: bool = False,
    ) -> bool:
        if turn.turn_task is not None:
            return True
        turn.turn_task = self._host._start_turn_task(
            session=turn.session,
            session_id=turn.session_id,
            message_id=turn.message_id,
            user_text=turn.user_text if user_text is None else user_text,
            workspace=workspace,
            unauthenticated_intake=unauthenticated_intake,
        )
        return turn.turn_task is not None

    async def _tracking_unavailable(self, turn: TurnContext) -> TurnOutcome:
        """Fail closed when an inbound message cannot obtain a task record."""

        _LOGGER.error(
            "session/prompt | sid=%s task tracking unavailable; Agent execution refused",
            turn.session_id,
        )
        await self._host._conn.session_update(
            session_id=turn.session_id,
            update=self._update_text(
                "任务跟踪不可用，消息未交给 Agent 处理；请让维护者检查任务存储。"
            ),
        )
        return TurnOutcome(
            response=PromptResponse(
                stop_reason="end_turn",
                user_message_id=turn.message_id,
            ),
            stop=True,
            reason="task_tracking_unavailable",
        )

    def _record_flow_transition(
        self,
        turn: TurnContext,
        *,
        kind: str,
        source_layer: str,
        target_layer: str,
        status: str,
        title: str,
        summary: str = "",
        evidence_level: str = "observed",
        decision: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """Record supplemental flow evidence without changing turn authority or outcome."""

        recorder = turn.turn_task
        if recorder is None:
            return
        try:
            recorder.record_event(
                "flow_transition",
                {
                    "kind": kind,
                    "source_layer": source_layer,
                    "target_layer": target_layer,
                    "status": status,
                    "evidence_level": evidence_level,
                    "title": title,
                    "summary": summary,
                    "decision": dict(decision or {}),
                    "payload": dict(payload or {}),
                },
            )
        except Exception:  # noqa: BLE001 - observability must not change message behavior
            _LOGGER.exception(
                "task flow event record failed | stage=%s task=%s",
                kind,
                getattr(recorder, "task_id", ""),
            )

    def _record_ingress_receipt(
        self,
        turn: TurnContext,
        *,
        workspace: Any,
        turn_identity: TurnIdentity | None,
    ) -> None:
        """Correlate optional QQ gateway evidence only after trusted identity parsing."""

        if self._platform_type != "qq":
            return
        chat_kind = "group" if str(getattr(workspace, "chat_kind", "")) == "group" else "p2p"
        actor_id = str(
            getattr(turn_identity, "sender_user_id", "")
            or getattr(workspace, "user_id", "")
            or ""
        )
        chat_id = str(getattr(workspace, "chat_id", "") or "")
        if chat_kind == "p2p" and not chat_id:
            chat_id = actor_id
        evidence_level = "missing"
        status = "unknown"
        title = "未关联到 QQ 接入网关收据"
        summary = "该缺口不影响既有身份与访问控制，也不会由任务内容反推。"
        decision: dict[str, object] = {
            "code": "ingress_receipt_unavailable",
            "authoritative": False,
        }
        try:
            root = receipt_root_from_env()
            if root is None:
                match = None
                decision["code"] = "receipt_store_not_configured"
            else:
                match = consume_ingress_receipt(
                    root,
                    platform="qq",
                    chat_kind=chat_kind,
                    chat_id=chat_id,
                    actor_id=actor_id,
                    content=turn.user_text,
                )
            if match is not None and match.status == "matched" and match.receipt is not None:
                receipt_decision = match.receipt.get("decision")
                safe_decision = (
                    dict(receipt_decision)
                    if isinstance(receipt_decision, Mapping)
                    else {}
                )
                decision = {
                    "code": str(safe_decision.get("code") or "forwarded"),
                    "outcome": "forward",
                    "allowed": True,
                    "authoritative": False,
                }
                evidence_level = "correlated"
                status = "succeeded"
                title = "QQ 接入网关允许并转发消息"
                summary = (
                    "通过会话、发送者和纯文本摘要精确关联；该收据仅用于观测，不参与授权。"
                )
            elif match is not None:
                decision["code"] = match.reason or match.status
        except (IngressReceiptError, OSError, ValueError) as exc:
            decision["code"] = "receipt_store_unavailable"
            _LOGGER.warning(
                "QQ ingress receipt correlation unavailable; authorization is unchanged | reason=%s",
                exc,
            )
        if evidence_level == "correlated":
            self._record_flow_transition(
                turn,
                kind="transport.onebot_message_received",
                source_layer="channel",
                target_layer="transport",
                status="succeeded",
                title="OneBot 入站消息已关联",
                summary="NapCat/OneBot 纯文本事件与当前可信回合摘要匹配。",
                evidence_level="correlated",
                decision={
                    "code": "onebot_text_correlated",
                    "authoritative": False,
                },
                payload={
                    "adapter": "qq",
                    "chat_kind": chat_kind,
                    "message_kind": "text",
                },
            )
        self._record_flow_transition(
            turn,
            kind="gateway.access_decision",
            source_layer="transport",
            target_layer="gateway",
            status=status,
            title=title,
            summary=summary,
            evidence_level=evidence_level,
            decision=decision,
            payload={
                "adapter": "qq",
                "chat_kind": chat_kind,
                "message_kind": "text",
                "correlation": evidence_level,
            },
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
            pending_attachment_names=getattr(self._host, "_attachment_ack_resource_names", {}).get(
                self._host._attachment_ack_key(
                    turn.session_id,
                    turn.session,
                ),
                [],
            ),
            send_task_status=self._host._send_task_status,
            send_job_status=self._host._send_job_status,
            send_unnotified_completed_jobs=(self._host._send_unnotified_completed_jobs),
            handle_code_task_control=self._host._handle_code_task_control,
            cancel_attachment_ack=self._host._cancel_attachment_ack,
            finish_turn_task=self._host._finish_turn_task,
            refresh_prompt_plan=self._refresh_prompt_plan,
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
            _attachment.collect_attachment_references(prompt_parts, turn.user_text)
            if self._has_user_files_pipeline
            else []
        )
        imported = (
            _attachment.import_transport_attachments(turn.session.workspace, referenced)
            if referenced
            else []
        )
        available = _attachment.confirmed_transport_attachments(
            turn.session.workspace,
            referenced,
            imported_names=imported,
        )
        turn.metadata["attachment_import_summary"] = {
            "requested": referenced,
            "imported": imported,
            "available": available,
        }
        turn.metadata["task_resources"] = ()

        if (
            getattr(turn.session.workspace, "scope", "actor") == WORKSPACE_SCOPE_GROUP_SHARED
            and referenced
            and len(available) < len(referenced)
        ):
            return await self._reject_unbound_group_attachment(turn, referenced)

        raw_prompt = turn.metadata["raw_prompt"]
        inline_images = _image.has_inline_images(raw_prompt)
        referenced_image_names = [name for name in referenced if is_supported_image_path(name)]
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
        if referenced_image_names and turn.user_text.strip() and not self._has_image_inputs:
            return await self._finish_image_turn(
                turn,
                text=("当前机器人未启用读图能力。这些图片仍可按普通文件保存，但不会交给模型分析。"),
                reason="image_inputs_disabled",
                progress="已说明读图能力未启用。",
            )
        if has_new_images and self._has_image_inputs and not turn.session.workspace.user_id:
            return await self._finish_image_turn(
                turn,
                text=("当前会话没有稳定用户身份，不能安全保存或分析图片。请重启机器人后重试。"),
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

        if not turn.metadata["task_resources"] and _attachment_turns.is_upload_only_prompt(
            has_user_files_pipeline=self._has_user_files_pipeline,
            prompt_parts=prompt_parts,
            user_text=turn.user_text,
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
        if turn.metadata["task_resources"] or _attachment.has_task_verb(turn.user_text):
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
            hint_names or referenced or prompt_parts.has_resource or turn.metadata["task_resources"]
        )
        turn.session = await self._host._ensure_agent_session(turn.session_id, turn.session)
        self._refresh_prompt_plan(turn.session)
        turn.metadata["task_metadata"] = build_topic_metadata(
            user_text=turn.user_text,
            chat_kind=turn.session.workspace.chat_kind,
            has_attachment=bool(turn.metadata["has_attachment"]),
            message_count=turn.session.message_count(),
        )
        turn.metadata["task_metadata"]["has_image"] = bool(turn.metadata["task_resources"])
        turn_identity = getattr(turn.session, "turn_identity", None)
        if turn_identity is not None:
            turn.metadata["task_metadata"].update(
                {
                    "conversation_platform": (turn_identity.conversation.platform),
                    "conversation_chat_kind": (turn_identity.conversation.chat_kind),
                    "turn_actor_ref": turn_identity.actor_ref,
                    "turn_identity_source": turn_identity.source,
                }
            )
        self._record_flow_transition(
            turn,
            kind="middleware.session_materialized",
            source_layer="middleware",
            target_layer="agent",
            status="succeeded",
            title="会话与输入已准备",
            summary="附件、上下文和本轮身份已在宿主边界完成处理。",
            payload={
                "adapter": self._platform_type,
                "chat_kind": str(turn.session.workspace.chat_kind or ""),
                "message_kind": "resource" if turn.metadata["task_resources"] else "text",
                "resource_count": len(turn.metadata["task_resources"]),
                "text_length": len(turn.user_text),
            },
        )
        return TurnOutcome()

    async def _reject_unbound_group_attachment(
        self,
        turn: TurnContext,
        referenced: list[str],
    ) -> TurnOutcome:
        text = _attachment.format_group_attachment_binding_rejection(referenced)
        await self._host._conn.session_update(
            session_id=turn.session_id,
            update=self._update_text(text),
        )
        resource_hint = "\n".join(f"[资源引用: {name}]" for name in referenced if name)
        accepted_text = "\n".join(part for part in (turn.user_text.strip(), resource_hint) if part)
        turn.session.record_exchange(accepted_text or "[文件上传]", text)
        self._host._cancel_attachment_ack(turn.session_id)
        self._host._finish_turn_task(
            turn.turn_task,
            progress="已拒绝无法绑定到当前群消息的附件。",
            final_text=text,
            stop_reason="end_turn",
        )
        return TurnOutcome(
            response=PromptResponse(
                stop_reason="end_turn",
                user_message_id=turn.message_id,
            ),
            stop=True,
            reason="group_attachment_identity_unavailable",
        )

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
                imported_names = _attachment.import_transport_attachments(
                    turn.session.workspace,
                    pending_names,
                )
            else:
                imported_names = []
            available_names = _attachment.confirmed_transport_attachments(
                turn.session.workspace,
                pending_names,
                imported_names=imported_names,
            )
            available_name_set = set(available_names)
            unresolved_names = [name for name in pending_names if name not in available_name_set]
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
            if len(resources) + len(unresolved_names) > _image.DEFAULT_IMAGE_INPUT_MAX_COUNT:
                raise ImageContentError("too many pending images for one model turn")
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
                    session=turn.session,
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
            session=turn.session,
        )
        text = _attachment.format_attachment_deferred_receipt(
            referenced,
            turn.session.workspace,
        )
        await self._host._conn.session_update(
            session_id=turn.session_id,
            update=self._update_text(text),
        )
        # Persist the accepted upload turn now, under the immutable identity
        # bound to this locked prompt. The asynchronous final acknowledgement
        # is delivery-only and may run after another message from this actor.
        resource_hint = "\n".join(f"[资源引用: {name}]" for name in referenced if name)
        accepted_text = "\n".join(part for part in (turn.user_text.strip(), resource_hint) if part)
        turn.session.record_exchange(
            accepted_text or "[文件上传]",
            text,
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
            response=PromptResponse(stop_reason="end_turn", user_message_id=turn.message_id),
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
            "task_turn_context": (getattr(turn.session, "turn_context", "") or None),
        }
        if task_resources:
            run_kwargs["task_resources"] = task_resources
        runtime = getattr(self._host, "_runtime", None)
        self._record_flow_transition(
            turn,
            kind="agent.task_submitted",
            source_layer="middleware",
            target_layer="agent",
            status="succeeded",
            title="任务已交给主 Agent",
            summary="后续模型、工具、子 Agent 和流程活动由统一 AgentEvent 契约记录。",
            payload={
                "backend": str(getattr(runtime, "agent_backend", "") or "unknown"),
                "resource_count": len(task_resources),
            },
        )
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
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
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
