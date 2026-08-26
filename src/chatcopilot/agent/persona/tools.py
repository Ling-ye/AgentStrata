"""Owner-only persona management tool and its session-bound provider."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, cast

from chatcopilot.agent.persona.draft_agent import PersonaDraftAgent, PersonaDraftLlm
from chatcopilot.agent.search.coordinator import SearchCoordinator
from chatcopilot.contracts.persona_control import (
    PendingPersonaProposal,
    PersonaDraftResult,
    PersonaMutationRequest,
)
from chatcopilot.contracts.identity import role_value
from chatcopilot.contracts.persistent_state import (
    PERSONA_MAX_ITEM_CHARS,
    PERSONA_SCOPES,
    PersistentConversationState,
    has_meaningful_persona,
)
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import (
    TOOL_AUDIENCE_MAIN,
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)
from chatcopilot.core.persona_control import PersonaControlService
from chatcopilot.core.workspace_runtime import normalize_chat_kind


_OPERATIONS = frozenset(
    {"show", "set", "append", "research", "refresh", "clear", "confirm", "cancel"}
)
_SCOPES = frozenset({"default", *PERSONA_SCOPES})
_CONFIRM_COMMAND = "/persona confirm"
_PROPOSAL_TTL_SECONDS = 600
_GLOBAL_CUES = ("全局", "所有会话", "所有群", "global", "every conversation")
_NAMED_PERSONA_RE = re.compile(
    r"(?:人格|人设)(?:为|是|成)|(?:你|机器人|助手)(?:就是|作为|扮演)|(?:模仿|扮演|冒充)",
    re.IGNORECASE,
)


class PersonaToolPort(Protocol):
    """Session-owned state that the Agent-layer tool may use without importing middleware."""

    @property
    def actor_id(self) -> str: ...

    @property
    def chat_id(self) -> str: ...

    def get_pending_proposal(self) -> PendingPersonaProposal | None: ...

    def set_pending_proposal(self, proposal: PendingPersonaProposal) -> None: ...

    def clear_pending_proposal(self) -> None: ...

    def refresh_prompt_plan(self) -> None: ...


SearchCoordinatorFactory = Callable[[], SearchCoordinator | None]


@dataclass(frozen=True)
class _PersonaManageHandler:
    port: PersonaToolPort
    llm: PersonaDraftLlm | None
    coordinator_factory: SearchCoordinatorFactory

    def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        operation = str(arguments.get("operation") or "").strip().lower()
        requested_scope = str(arguments.get("scope") or "default").strip().lower()
        if operation not in _OPERATIONS:
            return _failure(
                "persona_operation_invalid",
                "operation 不是受支持的人格操作。",
                operation=operation,
                scope=requested_scope,
            )
        if requested_scope not in _SCOPES:
            return _failure(
                "persona_scope_invalid",
                "scope 必须是 default、global、group 或 user。",
                operation=operation,
                scope=requested_scope,
            )
        if role_value(context.caller_role) != "owner":
            return _failure(
                "persona_owner_required",
                "人格配置仅限 Owner 管理。",
                operation=operation,
                scope=requested_scope,
            )
        state = cast(PersistentConversationState | None, context.persistent_state)
        if state is None:
            return _failure(
                "persona_state_unavailable",
                "当前会话没有可用的受保护人格状态。",
                operation=operation,
                scope=requested_scope,
            )
        workspace = context.workspace
        chat_kind = normalize_chat_kind(
            getattr(workspace, "chat_kind", None),
            getattr(workspace, "chat_id", None),
        )
        service = PersonaControlService(
            persistent_state=state,
            caller_role=context.caller_role,
            chat_kind=chat_kind,
        )

        if operation == "cancel":
            had_proposal = self.port.get_pending_proposal() is not None
            self.port.clear_pending_proposal()
            return ToolResult(
                ok=True,
                summary=(
                    "已取消待确认的人格提案。"
                    if had_proposal
                    else "当前没有待确认的人格提案。"
                ),
                data={
                    "outcome": "cancelled" if had_proposal else "unchanged",
                    "operation": operation,
                    "scope": requested_scope,
                    "committed": False,
                },
            )

        if operation == "confirm":
            return self._confirm(context=context, service=service, state=state)

        try:
            scope = service.resolve_scope(requested_scope)
        except ValueError:
            return _failure(
                "persona_scope_invalid",
                "人格作用域与当前会话不匹配。",
                operation=operation,
                scope=requested_scope,
            )

        if operation == "show":
            return _show_persona(state, chat_kind=chat_kind, requested_scope=requested_scope)

        request_text = str(context.request_text or "").strip()
        requirement = str(arguments.get("requirement") or "").strip()
        defer_confirmation = bool(arguments.get("defer_confirmation", False))
        if operation in {"set", "append", "research"}:
            if not requirement:
                return _failure(
                    "persona_requirement_empty",
                    "set、append 和 research 必须提供 requirement。",
                    operation=operation,
                    scope=scope,
                )
            if requirement not in request_text:
                return _failure(
                    "persona_requirement_ungrounded",
                    "requirement 必须是当前用户消息中的连续原文。",
                    operation=operation,
                    scope=scope,
                )
        elif requirement:
            return _failure(
                "persona_requirement_unexpected",
                f"{operation} 不接受 requirement。",
                operation=operation,
                scope=scope,
            )
        if scope == "global" and not any(cue in request_text for cue in _GLOBAL_CUES):
            return _failure(
                "persona_global_scope_ungrounded",
                "只有当前消息明确要求全局或所有会话时才能选择 global。",
                operation=operation,
                scope=scope,
            )

        if operation == "clear" or defer_confirmation:
            return self._defer(
                operation=operation,
                scope=scope,
                requirement=requirement,
                state=state,
            )

        self.port.clear_pending_proposal()
        return self._apply(
            operation=operation,
            scope=scope,
            requirement=requirement,
            service=service,
            state=state,
        )

    def _defer(
        self,
        *,
        operation: str,
        scope: str,
        requirement: str,
        state: PersistentConversationState,
    ) -> ToolResult:
        proposal_text = requirement
        if operation == "refresh":
            proposal_text = state.persona_snapshot(scope).strip()
            if not has_meaningful_persona(proposal_text):
                return _failure(
                    "persona_current_missing",
                    "当前作用域没有可刷新的有效人格。",
                    operation=operation,
                    scope=scope,
                )
        elif operation == "clear":
            proposal_text = ""
        base_persona = state.persona_snapshot(scope).strip()
        expires_at = time.time() + _PROPOSAL_TTL_SECONDS
        proposal = PendingPersonaProposal(
            operation=cast(Any, operation),
            scope=scope,
            text=proposal_text,
            content_sha256=_sha256(base_persona),
            actor_id=self.port.actor_id,
            chat_id=self.port.chat_id,
            expires_at=expires_at,
            requires_research=(
                operation in {"research", "refresh"}
                or _needs_enrichment(requirement)
            ),
        )
        self.port.set_pending_proposal(proposal)
        return ToolResult(
            ok=True,
            summary=(
                f"尚未修改 {scope} 人格。提案将在十分钟后失效；"
                f"如确认执行 {operation}，请发送精确 {_CONFIRM_COMMAND}。"
            ),
            data={
                "outcome": "confirmation_required",
                "operation": operation,
                "scope": scope,
                "committed": False,
                "proposal_expires_at": expires_at,
            },
        )

    def _confirm(
        self,
        *,
        context: ToolContext,
        service: PersonaControlService,
        state: PersistentConversationState,
    ) -> ToolResult:
        if str(context.request_text or "") != _CONFIRM_COMMAND:
            return _failure(
                "persona_confirmation_command_required",
                f"确认人格提案必须发送精确 {_CONFIRM_COMMAND}。",
                operation="confirm",
                scope="default",
            )
        proposal = self.port.get_pending_proposal()
        if proposal is None:
            return _failure(
                "persona_proposal_invalid",
                "当前没有可确认的人格提案。",
                operation="confirm",
                scope="default",
            )
        valid = (
            proposal.expires_at >= time.time()
            and proposal.actor_id == self.port.actor_id
            and proposal.chat_id == self.port.chat_id
        )
        if not valid:
            self.port.clear_pending_proposal()
            return _failure(
                "persona_proposal_invalid",
                "人格提案已失效或与当前 actor/chat 不匹配。",
                operation="confirm",
                scope=proposal.scope,
            )
        try:
            scope = service.resolve_scope(proposal.scope)
        except ValueError:
            self.port.clear_pending_proposal()
            return _failure(
                "persona_scope_invalid",
                "人格提案作用域与当前会话不匹配。",
                operation="confirm",
                scope=proposal.scope,
            )
        current = state.persona_snapshot(scope).strip()
        if _sha256(current) != proposal.content_sha256:
            self.port.clear_pending_proposal()
            return _failure(
                "persona_proposal_invalid",
                "待修改人格已发生变化，请重新发起操作。",
                operation="confirm",
                scope=scope,
            )
        self.port.clear_pending_proposal()
        if proposal.operation == "clear":
            return self._clear(service=service, scope=scope)
        return self._apply(
            operation=proposal.operation,
            scope=scope,
            requirement=proposal.text,
            service=service,
            state=state,
            research_required=proposal.requires_research,
        )

    def _clear(self, *, service: PersonaControlService, scope: str) -> ToolResult:
        receipt = service.execute(
            PersonaMutationRequest(operation="clear", scope=scope, confirm=True)
        )
        if not receipt.ok:
            return _failure(
                receipt.error_code or "persona_persistence_failed",
                "人格持久化失败，本轮没有修改。",
                operation="clear",
                scope=scope,
            )
        data = _receipt_data(
            outcome="cleared",
            operation="clear",
            receipt_operation=receipt.operation,
            scope=receipt.scope,
            content_sha256=receipt.content_sha256,
        )
        refresh_error = _refresh_error(self.port)
        if refresh_error is not None:
            return ToolResult(
                ok=False,
                summary="人格已清空，但当前会话 PromptPlan 刷新失败。",
                error="人格已持久化；当前会话刷新失败。",
                error_code="persona_prompt_refresh_failed",
                stage="refresh",
                details={"error_kind": refresh_error},
                data=data,
            )
        return ToolResult(
            ok=True,
            summary=f"已清空 {receipt.scope} 人格；内容哈希 {receipt.content_sha256[:16]}。",
            data=data,
        )

    def _apply(
        self,
        *,
        operation: str,
        scope: str,
        requirement: str,
        service: PersonaControlService,
        state: PersistentConversationState,
        research_required: bool | None = None,
    ) -> ToolResult:
        if self.llm is None:
            return _failure(
                "persona_research_model_unavailable",
                "人格草案模型不可用，本轮没有修改。",
                operation=operation,
                scope=scope,
            )
        current_persona = (
            state.persona_snapshot(scope).strip()
            if operation in {"append", "refresh"}
            else ""
        )
        if operation == "refresh":
            if not has_meaningful_persona(current_persona):
                return _failure(
                    "persona_current_missing",
                    "当前作用域没有可刷新的有效人格。",
                    operation=operation,
                    scope=scope,
                )
            requirement = current_persona
        requires_research = (
            research_required
            if research_required is not None
            else operation in {"research", "refresh"} or _needs_enrichment(requirement)
        )
        try:
            coordinator = self.coordinator_factory() if requires_research else None
            draft = PersonaDraftAgent(
                llm=self.llm,
                coordinator=coordinator,
                max_wall_seconds=90.0,
            ).draft(
                owner_requirement=requirement,
                operation="set" if operation == "research" else operation,
                current_persona=current_persona,
                research_required=requires_research,
            )
        except Exception as exc:  # noqa: BLE001 - no mutation has occurred
            return _failure(
                "persona_draft_failed",
                "人格草案未完成，本轮没有修改。",
                operation=operation,
                scope=scope,
                details={"error_kind": type(exc).__name__[:80]},
            )
        if not draft.ok:
            return _failure(
                draft.error_code or "persona_draft_failed",
                "人格草案未完成，本轮没有修改。",
                operation=operation,
                scope=scope,
                details=_draft_diagnostics(draft),
            )
        receipt = service.execute(
            PersonaMutationRequest(operation="set", scope=scope, text=draft.markdown)
        )
        if not receipt.ok:
            return _failure(
                receipt.error_code or "persona_persistence_failed",
                "人格持久化失败，本轮没有修改。",
                operation=operation,
                scope=scope,
                details=_draft_diagnostics(draft),
            )
        data = _receipt_data(
            outcome="saved",
            operation=operation,
            receipt_operation=receipt.operation,
            scope=receipt.scope,
            content_sha256=receipt.content_sha256,
        )
        data["draft"] = _draft_diagnostics(draft)
        refresh_error = _refresh_error(self.port)
        if refresh_error is not None:
            return ToolResult(
                ok=False,
                summary="人格已保存，但当前会话 PromptPlan 刷新失败。",
                error="人格已持久化；当前会话刷新失败。",
                error_code="persona_prompt_refresh_failed",
                stage="refresh",
                details={"error_kind": refresh_error},
                data=data,
            )
        source_suffix = (
            f" 草案使用了 {len(draft.source_urls)} 个公开来源。"
            if draft.source_urls
            else " 草案由 PersonaDraftAgent 完整生成。"
        )
        return ToolResult(
            ok=True,
            summary=(
                f"已设置 {receipt.scope} 人格；内容哈希 {receipt.content_sha256[:16]}。"
                + source_suffix
            ),
            data=data,
        )


def _show_persona(
    state: PersistentConversationState,
    *,
    chat_kind: str,
    requested_scope: str,
) -> ToolResult:
    if requested_scope == "default":
        raw_layers = state.persona_layers()
        resolved_scope = "group" if chat_kind == "group" else "user"
    else:
        text = state.persona_snapshot(requested_scope)
        raw_layers = (
            ((requested_scope, text),)
            if has_meaningful_persona(text)
            else ()
        )
        resolved_scope = requested_scope
    layers: list[dict[str, Any]] = []
    for scope, text in raw_layers:
        item: dict[str, Any] = {
            "scope": scope,
            "content_sha256": _sha256(text),
        }
        if chat_kind != "group":
            item["markdown"] = text.strip()
        layers.append(item)
    if not layers:
        summary = "当前会话未设置有效人格。"
    elif chat_kind == "group":
        summary = "当前群人格已启用；群聊中不公开底层正文。"
    else:
        summary = "当前生效人格（后层优先）：\n\n" + "\n\n".join(
            f"## {item['scope']} 层\n{item['markdown']}" for item in layers
        )
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "outcome": "shown",
            "operation": "show",
            "scope": resolved_scope,
            "committed": False,
            "layers": layers,
        },
    )


def _failure(
    error_code: str,
    message: str,
    *,
    operation: str,
    scope: str,
    details: Mapping[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        ok=False,
        error=message,
        error_code=error_code,
        stage="persona",
        details=dict(details or {}),
        data={
            "outcome": "failed",
            "operation": operation,
            "scope": scope,
            "committed": False,
        },
    )


def _receipt_data(
    *,
    outcome: str,
    operation: str,
    receipt_operation: str,
    scope: str,
    content_sha256: str,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "operation": operation,
        "scope": scope,
        "committed": True,
        "content_sha256": content_sha256,
        "receipt": {
            "operation": receipt_operation,
            "scope": scope,
            "content_sha256": content_sha256,
        },
    }


def _draft_diagnostics(draft: PersonaDraftResult) -> dict[str, Any]:
    return {
        "model": draft.model,
        "model_calls": len(draft.calls),
        "search_calls": draft.search_calls,
        "elapsed_ms": draft.elapsed_ms,
        "source_urls": list(draft.source_urls),
        "observed_source_count": len(draft.observed_source_urls),
        "usage": dict(draft.usage),
        "error_code": draft.error_code,
        "error_kind": draft.error_kind,
    }


def _refresh_error(port: PersonaToolPort) -> str | None:
    try:
        port.refresh_prompt_plan()
    except Exception as exc:  # noqa: BLE001 - the mutation receipt remains authoritative
        return type(exc).__name__[:80]
    return None


def _needs_enrichment(requirement: str) -> bool:
    return bool(_NAMED_PERSONA_RE.search(requirement or "")) and not any(
        phrase in requirement for phrase in ("更简洁", "更温柔", "更活泼", "更专业")
    )


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


_INPUT_SCHEMA = object_schema(
    {
        "operation": {
            "type": "string",
            "enum": sorted(_OPERATIONS),
            "description": (
                "show=查看；set=替换；append=合并补充；research=检索命名人物后替换；"
                "refresh=重查并整理；clear=创建清空提案；confirm=确认提案；cancel=取消提案。"
            ),
        },
        "scope": {
            "type": "string",
            "enum": sorted(_SCOPES),
            "default": "default",
            "description": "default 自动绑定当前私聊 user 或当前 group；global 必须由用户明确提出。",
        },
        "requirement": {
            "type": "string",
            "minLength": 1,
            "maxLength": PERSONA_MAX_ITEM_CHARS,
            "description": (
                "set/append/research 的人格要求，必须逐字取自当前用户消息中的一个连续子串。"
            ),
        },
        "defer_confirmation": {
            "type": "boolean",
            "default": False,
            "description": (
                "要求依赖前文、指代或仍不够确定时设为 true；工具只创建 actor-bound 提案。"
            ),
        },
    },
    required=("operation",),
)

_OUTPUT_SCHEMA = object_schema(
    {
        "outcome": {
            "type": "string",
            "enum": [
                "shown",
                "saved",
                "cleared",
                "confirmation_required",
                "cancelled",
                "unchanged",
                "failed",
            ],
        },
        "operation": {"type": "string"},
        "scope": {"type": "string"},
        "committed": {"type": "boolean"},
        "content_sha256": {"type": "string"},
        "proposal_expires_at": {"type": "number"},
        "layers": {
            "type": "array",
            "items": object_schema(
                {
                    "scope": {"type": "string"},
                    "content_sha256": {"type": "string"},
                    "markdown": {"type": "string"},
                },
                required=("scope", "content_sha256"),
            ),
        },
        "receipt": object_schema(
            {
                "operation": {"type": "string"},
                "scope": {"type": "string"},
                "content_sha256": {"type": "string"},
            },
            required=("operation", "scope", "content_sha256"),
        ),
        "draft": object_schema(
            {
                "model": {"type": "string"},
                "model_calls": {"type": "integer"},
                "search_calls": {"type": "integer"},
                "elapsed_ms": {"type": "integer"},
                "source_urls": {"type": "array", "items": {"type": "string"}},
                "observed_source_count": {"type": "integer"},
                "usage": {"type": "object"},
                "error_code": {"type": "string"},
                "error_kind": {"type": "string"},
            },
            required=(
                "model",
                "model_calls",
                "search_calls",
                "elapsed_ms",
                "source_urls",
                "observed_source_count",
                "usage",
                "error_code",
                "error_kind",
            ),
        ),
    },
    required=("outcome", "operation", "scope", "committed"),
)


def build_persona_provider(
    port: PersonaToolPort,
    *,
    llm: PersonaDraftLlm | None,
    coordinator_factory: SearchCoordinatorFactory,
) -> ToolProvider:
    """Build the session-bound provider selected by the ``persona.control`` pack."""

    tool = ToolDef(
        name="persona_manage",
        summary=(
            "Owner-only 人格管理。用户用自然语言或 /persona 要求查看、设置、补充、检索、"
            "刷新、清空、确认或取消持续人格时必须调用。set/append/research 的 requirement 必须"
            "是当前用户消息的连续原文；命名人物或角色优先用 research。依赖指代或含义不确定时"
            "设置 defer_confirmation=true。clear 永远只创建提案，confirm 仅接受用户精确发送"
            " /persona confirm。只有 data.committed=true 才能声称人格已保存或清空。"
        ),
        input_schema=_INPUT_SCHEMA,
        output_schema=_OUTPUT_SCHEMA,
        handler=_PersonaManageHandler(
            port=port,
            llm=llm,
            coordinator_factory=coordinator_factory,
        ),
        aliases=["人格管理", "persona"],
        requires_role="owner",
        weight="heavy",
        category="agent.persona",
        owner="agent",
        module=__name__,
        artifact_kinds=(),
        audiences=(TOOL_AUDIENCE_MAIN,),
    )
    return ToolProvider(
        id="persona",
        module=__name__,
        description="Owner-only session-bound persona management.",
        packs={"persona.control": (tool,)},
    )


__all__ = ["PersonaToolPort", "build_persona_provider"]
