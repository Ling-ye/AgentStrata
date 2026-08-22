"""Single trusted ACP entry point for persona management."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable

from acp import PromptResponse

from chatcopilot.agent.persona.draft_agent import PersonaDraftAgent
from chatcopilot.agent.persona.interpreter import (
    PersonaCandidateDetector,
    PersonaInterpreter,
    explicit_persona_directive,
    parse_persona_command,
)
from chatcopilot.contracts.identity import role_value
from chatcopilot.contracts.persona_control import (
    PersonaDirective,
    PersonaDraftResult,
    PersonaMutationRequest,
    PendingPersonaProposal,
)
from chatcopilot.contracts.persistent_state import has_meaningful_persona
from chatcopilot.core.persona_control import PersonaControlService
from chatcopilot.middleware.acp.workspace_service import build_workspace_service


_DENIED = (
    "机器人自身人格仅限 Owner 管理，本轮没有修改。"
    "普通格式要求和独立角色内容创作仍可正常提出。"
)
_HELP = """人格命令：
/persona<自然语言人格要求>
/persona show [global|group|user]
/persona set [global|group|user] <人格要求>
/persona append [global|group|user] <补充要求>
/persona research [global|group|user] <自然语言要求>
/persona refresh [global|group|user]
/persona clear [global|group|user]
/persona confirm
/persona cancel

群聊默认 group，私聊默认 user。自然语言也可以直接表达长期人格要求。"""


async def handle_persona_control(
    *,
    host: Any,
    turn: Any,
    update_text: Callable[[str], Any],
    refresh_prompt_plan: Callable[[Any], None],
) -> PromptResponse | None:
    """Interpret once, let one Agent draft, then authorize one host mutation."""

    spec = getattr(getattr(turn.session, "runtime", None), "subagents", None)
    control = getattr(spec, "persona_control", None)
    if not bool(getattr(control, "enabled", False)):
        return None

    original_text = turn.user_text
    directive = parse_persona_command(original_text)
    agent_runtime = None
    started = time.time()
    if directive is None:
        candidate = PersonaCandidateDetector().detect(original_text)
        if candidate == "none":
            return None
        if role_value(getattr(turn.session, "role", "user")) != "owner":
            return await _finish(
                host,
                turn,
                update_text,
                _DENIED,
                progress="已拒绝非 Owner 人格修改。",
                persona_outcome="denied",
            )
        if candidate == "explicit":
            directive = explicit_persona_directive(original_text)
        else:
            try:
                agent_runtime = await asyncio.to_thread(host._get_or_build_agent_runtime)
                previous_user, previous_assistant = _previous_exchange(turn.session)
                directive = await asyncio.to_thread(
                    PersonaInterpreter(agent_runtime.llm).interpret,
                    current_message=original_text,
                    previous_user=previous_user,
                    previous_assistant=previous_assistant,
                    chat_kind=str(turn.session.workspace.chat_kind or ""),
                )
            except Exception:  # noqa: BLE001 - ambiguous requests fail closed
                return await _finish(
                    host,
                    turn,
                    update_text,
                    "人格意图判定失败，本轮没有修改。请使用精确 /persona 命令重试。",
                    status="failed",
                    progress="人格意图判定失败。",
                    error_code="persona_interpretation_failed",
                    persona_outcome="failed",
                )
    _record_decision(turn, directive, started_at=started)

    if not directive.handles_turn or directive.confidence == "low":
        return None
    if directive.operation == "help":
        return await _finish(
            host,
            turn,
            update_text,
            _HELP,
            progress="已返回人格命令帮助。",
            persona_outcome="help",
        )
    if role_value(getattr(turn.session, "role", "user")) != "owner":
        return await _finish(
            host,
            turn,
            update_text,
            _DENIED,
            progress="已拒绝非 Owner 人格修改。",
            persona_outcome="denied",
        )

    if directive.operation == "cancel":
        turn.session.pending_persona_proposal = None
        return await _finish(
            host,
            turn,
            update_text,
            "已取消待确认的人格提案。",
            progress="已取消人格提案。",
            persona_outcome="cancelled",
        )
    if directive.operation == "confirm":
        proposal = turn.session.pending_persona_proposal
        actor_id = str(turn.session.workspace.user_id or "")
        chat_id = str(turn.session.workspace.chat_id or "")
        if (
            proposal is None
            or proposal.expires_at < time.time()
            or proposal.actor_id != actor_id
            or proposal.chat_id != chat_id
            or hashlib.sha256(proposal.text.encode("utf-8")).hexdigest()
            != proposal.content_sha256
        ):
            turn.session.pending_persona_proposal = None
            return await _finish(
                host,
                turn,
                update_text,
                "没有可确认的人格提案，或提案已经失效。",
                status="failed",
                progress="人格提案不存在或已失效。",
                error_code="persona_proposal_invalid",
                persona_outcome="failed",
            )
        turn.session.pending_persona_proposal = None
        directive = PersonaDirective(
            operation=proposal.operation,
            confidence="high",
            scope=proposal.scope,  # type: ignore[arg-type]
            text=proposal.text,
            enrich=proposal.requires_research,
            source="confirmation",
            reason="actor-bound proposal confirmed",
        )

    persistent_state = build_workspace_service(
        turn.session.workspace,
        str(getattr(turn.session.runtime, "platform_type", "unknown") or "unknown"),
    ).resolve_persistent_state()
    service = PersonaControlService(
        persistent_state=persistent_state,
        caller_role=getattr(turn.session, "role", "user"),
        chat_kind=turn.session.workspace.chat_kind,
    )
    try:
        scope = service.resolve_scope(directive.scope)
    except ValueError:
        return await _finish(
            host,
            turn,
            update_text,
            "人格作用域与当前会话不匹配，本轮没有修改。",
            status="failed",
            progress="人格作用域校验失败。",
            error_code="persona_scope_invalid",
            persona_outcome="failed",
        )

    if directive.operation == "show":
        return await _finish(
            host,
            turn,
            update_text,
            _show_persona(persistent_state, turn.session, directive.scope),
            progress="已查询人格状态。",
            persona_outcome="shown",
        )
    if directive.operation == "clear":
        if directive.confidence != "high":
            empty_hash = hashlib.sha256(b"").hexdigest()
            turn.session.pending_persona_proposal = PendingPersonaProposal(
                operation="clear",
                scope=scope,
                text="",
                content_sha256=empty_hash,
                actor_id=str(turn.session.workspace.user_id or ""),
                chat_id=str(turn.session.workspace.chat_id or ""),
                expires_at=time.time() + 600,
                requires_research=False,
            )
            return await _finish(
                host,
                turn,
                update_text,
                f"尚未清空 {scope} 人格。提案将在十分钟后失效；"
                "如确认清空，请发送 /persona confirm。",
                progress="人格清空等待 actor-bound 精确确认。",
                persona_outcome="confirmation_required",
            )
        receipt = _mutate(turn, service, operation="clear", scope=scope, text="", confirm=True)
        return await _finish_mutation(
            host=host,
            turn=turn,
            update_text=update_text,
            refresh_prompt_plan=refresh_prompt_plan,
            receipt=receipt,
            success_text=f"已清空 {scope} 人格；内容哈希 {receipt.content_sha256[:16]}。",
        )

    if directive.confidence == "medium":
        proposal_operation = (
            directive.operation
            if directive.operation in {"set", "append", "research"}
            else "set"
        )
        proposal_text = directive.text.strip() or original_text.strip()
        turn.session.pending_persona_proposal = PendingPersonaProposal(
            operation=proposal_operation,  # type: ignore[arg-type]
            scope=scope,
            text=proposal_text,
            content_sha256=hashlib.sha256(proposal_text.encode("utf-8")).hexdigest(),
            actor_id=str(turn.session.workspace.user_id or ""),
            chat_id=str(turn.session.workspace.chat_id or ""),
            expires_at=time.time() + 600,
            requires_research=(directive.operation == "research" or directive.enrich),
        )
        return await _finish(
            host,
            turn,
            update_text,
            "这条要求依赖指代或含义不够确定，本轮没有保存。"
            "提案将在十分钟后失效；如确认按此生效，请发送 /persona confirm。",
            progress="人格要求存在歧义，等待精确确认。",
            persona_outcome="confirmation_required",
        )

    current_persona = (
        persistent_state.persona_snapshot(scope).strip()
        if directive.operation in {"append", "refresh"}
        else ""
    )
    owner_requirement = directive.text.strip()
    if directive.operation == "refresh":
        owner_requirement = "重新核实并整理当前人格，输出一份完整、连贯的人格文件。"
    if not owner_requirement:
        return await _finish(
            host,
            turn,
            update_text,
            _HELP,
            status="failed",
            progress="人格文本为空。",
            error_code="persona_text_empty",
            persona_outcome="failed",
        )
    if agent_runtime is None:
        try:
            agent_runtime = await asyncio.to_thread(host._get_or_build_agent_runtime)
        except Exception:  # noqa: BLE001 - no write has occurred
            agent_runtime = None
    requires_research = directive.operation in {"research", "refresh"} or directive.enrich
    draft = await _draft(
        agent_runtime,
        owner_requirement=owner_requirement,
        operation=directive.operation,
        current_persona=current_persona,
        research_required=requires_research,
    )
    _record_draft(turn, draft)
    if not draft.ok:
        return await _finish(
            host,
            turn,
            update_text,
            "人格草案未完成，本轮没有保存。"
            f"缺口：{draft.error_code or 'unknown'}。",
            status="failed",
            progress="人格草案 Agent 未通过生成或校验。",
            error_code=draft.error_code or "persona_draft_failed",
            persona_outcome="failed",
        )

    final_receipt = _mutate(
        turn,
        service,
        operation="set",
        scope=scope,
        text=draft.markdown,
    )
    if not final_receipt.ok:
        return await _finish(
            host,
            turn,
            update_text,
            "人格持久化失败，本轮没有修改。",
            status="failed",
            progress="人格持久化失败。",
            error_code=final_receipt.error_code,
            persona_outcome="failed",
        )
    if turn.session.is_materialized:
        refresh_prompt_plan(turn.session)

    success = _success_text(final_receipt, draft)
    if directive.residual_text:
        turn.metadata["persona_final_prefix"] = success
        turn.metadata["journal_user_text"] = original_text
        turn.user_text = directive.residual_text
        _set_outcome(turn, "persisted", "")
        return None
    return await _finish(
        host,
        turn,
        update_text,
        success,
        progress="已完成人格持久化。",
        persona_outcome="persisted",
    )


async def _draft(
    agent_runtime: Any,
    *,
    owner_requirement: str,
    operation: str,
    current_persona: str,
    research_required: bool,
) -> PersonaDraftResult:
    if agent_runtime is None:
        return PersonaDraftResult(error_code="persona_runtime_unavailable")
    try:
        coordinator = agent_runtime.build_unified_search_coordinator(max_wall_seconds=60.0)
        research_llm = getattr(agent_runtime, "research_llm", None)
        if research_llm is None:
            return PersonaDraftResult(error_code="persona_research_model_unavailable")
        return await asyncio.to_thread(
            PersonaDraftAgent(
                llm=research_llm,
                coordinator=coordinator,
                max_wall_seconds=90.0,
            ).draft,
            owner_requirement=owner_requirement,
            operation=operation,
            current_persona=current_persona,
            research_required=research_required,
        )
    except Exception as exc:  # noqa: BLE001 - no mutation has occurred
        return PersonaDraftResult(
            model=str(getattr(getattr(agent_runtime, "research_llm", None), "model", "")),
            error_code="persona_draft_failed",
            error_kind=type(exc).__name__[:80],
        )


async def _finish_mutation(
    *,
    host: Any,
    turn: Any,
    update_text: Callable[[str], Any],
    refresh_prompt_plan: Callable[[Any], None],
    receipt: Any,
    success_text: str,
) -> PromptResponse:
    if not receipt.ok:
        return await _finish(
            host,
            turn,
            update_text,
            "人格持久化失败，本轮没有修改。",
            status="failed",
            progress="人格持久化失败。",
            error_code=receipt.error_code,
            persona_outcome="failed",
        )
    if turn.session.is_materialized:
        refresh_prompt_plan(turn.session)
    return await _finish(
        host,
        turn,
        update_text,
        success_text,
        progress="已完成人格持久化。",
        persona_outcome="persisted",
    )


def _mutate(
    turn: Any,
    service: PersonaControlService,
    *,
    operation: str,
    scope: str,
    text: str,
    confirm: bool = False,
) -> Any:
    receipt = service.execute(
        PersonaMutationRequest(
            operation=operation,  # type: ignore[arg-type]
            scope=scope,
            text=text,
            confirm=confirm,
        )
    )
    if turn.turn_task is not None:
        turn.turn_task.record_event(
            "persona_mutation",
            {
                "ok": receipt.ok,
                "operation": receipt.operation,
                "scope": receipt.scope,
                "content_sha256": receipt.content_sha256,
                "error_code": receipt.error_code,
            },
        )
    return receipt


def _show_persona(persistent_state: Any, session: Any, requested_scope: str) -> str:
    if requested_scope == "default":
        layers = persistent_state.persona_layers()
    else:
        text = persistent_state.persona_snapshot(requested_scope)
        layers = ((requested_scope, text),) if has_meaningful_persona(text) else ()
    if not layers:
        return "当前会话未设置有效人格。"
    is_group = str(getattr(session.workspace, "chat_kind", "") or "").lower() == "group"
    if is_group:
        rows = [
            f"{scope}: enabled=true, version=sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
            for scope, text in layers
        ]
        return "当前群人格状态（不公开底层正文）：\n" + "\n".join(rows)
    return "当前生效人格（后层优先）：\n\n" + "\n\n".join(
        f"## {scope} 层\n{text.strip()}" for scope, text in layers
    )


def _previous_exchange(session: Any) -> tuple[str, str]:
    previous_user = ""
    previous_assistant = ""
    for message in reversed(list(getattr(session, "_messages", []) or [])):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "assistant" and not previous_assistant:
            previous_assistant = content
        elif role == "user" and not previous_user:
            previous_user = content
        if previous_user and previous_assistant:
            break
    return previous_user, previous_assistant


def _record_decision(turn: Any, directive: PersonaDirective, *, started_at: float) -> None:
    if turn.turn_task is None:
        return
    method = getattr(turn.turn_task, "persona_decision", None)
    if callable(method):
        method(
            operation=directive.operation,
            confidence=directive.confidence,
            scope=directive.scope,
            reason=directive.reason,
            source=directive.source,
            model=directive.model,
            usage=dict(directive.usage or {}),
            started_at=started_at,
        )


def _record_draft(turn: Any, result: PersonaDraftResult) -> None:
    if turn.turn_task is None:
        return
    method = getattr(turn.turn_task, "persona_draft", None)
    if callable(method):
        method(result=result)
        return
    turn.turn_task.record_event("persona_draft", _draft_event_payload(result))


def _success_text(receipt: Any, draft: PersonaDraftResult) -> str:
    base = f"已设置 {receipt.scope} 人格；内容哈希 {receipt.content_sha256[:16]}。"
    if draft.source_urls:
        return base + f" 草案 Agent 使用了 {len(draft.source_urls)} 个公开来源。"
    return base + " 草案由人格 Agent 完整生成。"


def _draft_event_payload(result: PersonaDraftResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "model": result.model,
        "model_calls": len(result.calls),
        "search_calls": result.search_calls,
        "source_urls": list(result.source_urls),
        "source_count": len(result.source_urls),
        "observed_source_count": len(result.observed_source_urls),
        "elapsed_ms": result.elapsed_ms,
        "error_code": result.error_code,
        "error_kind": result.error_kind,
        "markdown_sha256": (
            hashlib.sha256(result.markdown.encode("utf-8")).hexdigest()
            if result.markdown
            else ""
        ),
    }


def _set_outcome(turn: Any, outcome: str, error_code: str) -> None:
    if turn.turn_task is None:
        return
    setter = getattr(turn.turn_task, "set_persona_outcome", None)
    if callable(setter):
        setter(outcome=outcome, error_code=error_code)


async def _finish(
    host: Any,
    turn: Any,
    update_text: Callable[[str], Any],
    text: str,
    *,
    status: str = "succeeded",
    progress: str,
    error_code: str = "",
    persona_outcome: str = "",
) -> PromptResponse:
    _set_outcome(turn, persona_outcome, error_code)
    await host._conn.session_update(
        session_id=turn.session_id,
        update=update_text(text),
    )
    turn.session.record_exchange(turn.user_text, text)
    host._finish_turn_task(
        turn.turn_task,
        status=status,
        progress=progress,
        final_text=text,
        stop_reason="end_turn",
        error=error_code if status == "failed" else "",
    )
    return PromptResponse(stop_reason="end_turn", user_message_id=turn.message_id)


__all__ = ["handle_persona_control"]
