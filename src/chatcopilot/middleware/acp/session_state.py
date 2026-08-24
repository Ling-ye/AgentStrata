"""ACP 单个会话的状态容器。

middleware/acp 内部用 ``SessionState`` 承载一次 ACP session 的全部上下文：
``Workspace`` + ``Role`` + ``AgentSession`` + 当前业务模式/调试模式 + bot runtime
快照（用于重建 PromptPlan）。这样 ACP server 的各个 handler 不直接持有
``AgentSession``，所有"角色 / 模式 / workspace"语义都通过本对象访问。

设计意图：
- ``AgentSession`` 是 agent 层的纯 chat loop，对上不感知 Role / 模式 / 平台。
- 各种平台 / 协议语义（debug_mode / assistant_mode / workspace / role）由 ACP
  层在 ``SessionState`` 里维持。
- transcript 落盘由 middleware 负责，通过 ``persist_transcript()`` 在每轮末尾调用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from uuid import uuid4

from chatcopilot.agent.session_protocol import AgentSessionProtocol
from chatcopilot.botspec import BotRuntimeContext
from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.contracts.identity import TurnIdentity
from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.contracts.model_selection import (
    CodeModelSelection,
    MODEL_SELECTION_SCOPE_ONCE,
    MODEL_SELECTION_SCOPE_SESSION,
)
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.persona_control import PendingPersonaProposal
from chatcopilot.middleware.access_control import AssistantMode, Role
from chatcopilot.core.workspace_runtime import Workspace, cleanup_workspace

if TYPE_CHECKING:
    from chatcopilot.core.config import RoutingConfig
    from chatcopilot.middleware.acp.group_conversation import GroupConversationJournal

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.session_state")


def _sanitize_session_id(value: str) -> str:
    return (
        "".join(ch if (ch.isalnum() or ch in "-_.@") else "_" for ch in value).strip("_")
        or "session"
    )


@dataclass
class SessionState:
    """单次 ACP 会话的完整状态。"""

    session_id: str
    workspace: Workspace
    role: Role
    assistant_mode: AssistantMode
    runtime: BotRuntimeContext
    session: AgentSessionProtocol | None = None
    llm_model: str | None = None
    routing_config: RoutingConfig | None = None
    execution_session_id: str | None = None
    code_model_selection: CodeModelSelection | None = None
    code_model_once: CodeModelSelection | None = None
    debug_mode: bool = False
    pending_image_resources: tuple[ResourceRef, ...] = field(
        default=(),
        repr=False,
    )
    pending_image_names: tuple[str, ...] = field(default=(), repr=False)
    turn_identity: TurnIdentity | None = field(default=None, repr=False)
    conversation_journal: GroupConversationJournal | None = field(
        default=None,
        repr=False,
    )
    conversation_cursor: int = field(default=0, repr=False)
    turn_context: str = field(default="", repr=False)
    pending_persona_proposal: PendingPersonaProposal | None = field(default=None, repr=False)
    _transcript_path: Optional[Path] = field(default=None, repr=False)
    _pending_exchanges: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _workspace_materialized: bool = field(default=False, init=False, repr=False)

    @property
    def is_workspace_materialized(self) -> bool:
        return self._workspace_materialized

    def materialize_workspace(self) -> bool:
        """Create runtime storage once, after the caller has admitted the turn."""

        if self._workspace_materialized:
            return False
        self.workspace = self.workspace.ensure()
        cleanup_workspace(self.workspace)
        self._workspace_materialized = True
        self._initialize_transcript_path()
        return True

    def _initialize_transcript_path(self) -> None:
        # The conversation-level placeholder exists before a sender passes the
        # admission boundary. It does not own actor diagnostics.
        if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED and (
            not self.workspace.user_id or not self.execution_session_id
        ):
            self._transcript_path = None
            return
        if self._transcript_path is None:
            try:
                transcript_root = self.workspace.transcripts
                if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED:
                    state_root = self.workspace.root.parent / ".conversation-state"
                    if state_root.is_symlink():
                        raise RuntimeError("shared-group conversation state must not be a symlink")
                    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    state_root.chmod(0o700)
                    transcript_root = state_root / "transcripts"
                    if transcript_root.is_symlink():
                        raise RuntimeError("shared-group transcript root must not be a symlink")
                    transcript_root.mkdir(mode=0o700, exist_ok=True)
                    transcript_root.chmod(0o700)
                else:
                    transcript_root.mkdir(parents=True, exist_ok=True)
                start_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                transcript_id = self.execution_session_id or self.session_id
                if self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED:
                    transcript_id = f"{self.session_id}.actor.{uuid4().hex}"
                fname = f"{_sanitize_session_id(transcript_id)}__{start_iso}.jsonl"
                self._transcript_path = transcript_root / fname
            except (OSError, RuntimeError):
                _LOGGER.exception("无法初始化 transcript 路径，本会话不落盘")
                self._transcript_path = None

    @property
    def skill_index(self) -> tuple[SkillIndexEntry, ...]:
        if self.runtime is None:
            return ()
        return getattr(self.runtime, "skills", ())

    # ------------------------------------------------------------------
    # AgentSession 的便捷代理（让上层无需访问 .session 内部）
    # ------------------------------------------------------------------
    @property
    def is_materialized(self) -> bool:
        return self.session is not None

    def require_session(self) -> AgentSessionProtocol:
        if self.session is None:
            raise RuntimeError("Agent session has not been materialized")
        return self.session

    def attach_session(self, session: AgentSessionProtocol) -> None:
        if self.session is session:
            return
        if self.session is not None:
            raise RuntimeError("Agent session is already materialized")
        if not self._workspace_materialized:
            raise RuntimeError("Workspace must be materialized before the Agent session")
        for user_text, assistant_text in self._pending_exchanges:
            session.record_exchange(user_text, assistant_text)
        self._pending_exchanges.clear()
        self.session = session
        self.persist_transcript()

    def message_count(self) -> int:
        if self.session is None:
            return len(self._pending_exchanges) * 2
        return self.session.message_count

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        """记录未进入 LLM 工具循环的确定性回复，并落 transcript。"""
        group_sequence: int | None = None
        if self.turn_identity is not None and self.conversation_journal is not None:
            # Commit the protected conversation record before advancing this
            # actor's backend. If the journal is unavailable, the backend stays
            # untouched and the turn fails closed instead of forking history.
            group_sequence = self.conversation_journal.append(
                identity=self.turn_identity,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        if self.session is None:
            self._pending_exchanges.append((user_text, assistant_text))
        else:
            try:
                self.session.record_exchange(user_text, assistant_text)
            except Exception:
                # The journal is authoritative and the cursor deliberately
                # remains behind. Drop a potentially half-mutated native resume;
                # after rematerialization the journal delta reconstructs the
                # accepted deterministic exchange exactly once.
                failed_session = self.session
                self.session = None
                discard = getattr(failed_session, "discard", None)
                close = getattr(failed_session, "close", None)
                action = discard if callable(discard) else close
                if callable(action):
                    try:
                        action()
                    except Exception:  # noqa: BLE001 - preserve original failure
                        _LOGGER.exception("failed to discard inconsistent group backend")
                raise
        if group_sequence is not None:
            # This deterministic exchange is now present in both the journal
            # and this actor's backend. Advance so only newer actors' turns are
            # injected on the next prompt.
            self.conversation_cursor = group_sequence
        self.persist_transcript()

    def bind_group_turn(
        self,
        *,
        identity: TurnIdentity,
        journal: "GroupConversationJournal",
        turn_context: str,
    ) -> None:
        self.turn_identity = identity
        self.conversation_journal = journal
        self.turn_context = turn_context

    def record_group_model_exchange(
        self,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Persist one model turn without duplicating it in the backend history."""

        self._append_group_exchange(user_text, assistant_text, advance_cursor=True)
        self.persist_transcript()

    def _append_group_exchange(
        self,
        user_text: str,
        assistant_text: str,
        *,
        advance_cursor: bool,
    ) -> None:
        if self.turn_identity is None or self.conversation_journal is None:
            return
        sequence = self.conversation_journal.append(
            identity=self.turn_identity,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        if advance_cursor:
            self.conversation_cursor = sequence

    def set_code_model_selection(self, selection: CodeModelSelection) -> None:
        """Store a session or one-shot Codex selection without touching the chat LLM."""
        if selection.scope == MODEL_SELECTION_SCOPE_ONCE:
            self.code_model_once = selection
        elif selection.scope == MODEL_SELECTION_SCOPE_SESSION:
            self.code_model_selection = selection
        else:
            raise ValueError(f"unsupported model-selection scope: {selection.scope}")
        self.persist_transcript()

    def clear_code_model_selection(self) -> None:
        self.code_model_selection = None
        self.code_model_once = None
        self.persist_transcript()

    def effective_code_model_selection(
        self,
        default: CodeModelSelection,
    ) -> CodeModelSelection:
        return self.code_model_once or self.code_model_selection or default

    def consume_code_model_once(self, selection: CodeModelSelection) -> None:
        """Consume a one-shot selection only after its job was queued successfully."""
        if self.code_model_once == selection:
            self.code_model_once = None
            self.persist_transcript()

    def copy_code_model_state_from(self, other: "SessionState") -> None:
        """Preserve conversational model overrides across workspace identity refreshes."""
        self.code_model_selection = other.code_model_selection
        self.code_model_once = other.code_model_once

    def set_assistant_mode(
        self,
        mode: AssistantMode,
    ) -> None:
        """Change the mode value; the caller rebuilds the single PromptPlan."""
        self.assistant_mode = mode

    @property
    def _messages(self) -> list:
        """直接代理 AgentSession 的内部 messages 列表（供 transcript / 调试只读）。"""
        if self.session is not None:
            return self.session._messages
        messages: list[dict[str, str]] = []
        for user_text, assistant_text in self._pending_exchanges:
            messages.extend(
                [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": assistant_text},
                ]
            )
        return messages

    # ------------------------------------------------------------------
    # Transcript 落盘
    # ------------------------------------------------------------------
    @property
    def transcript_path(self) -> Optional[Path]:
        return self._transcript_path

    def persist_transcript(self) -> None:
        """把当前 messages 整体覆写到 transcript JSONL（每轮调用一次）。"""
        if not self._workspace_materialized or self._transcript_path is None:
            return
        shared_group = self.workspace.scope == WORKSPACE_SCOPE_GROUP_SHARED
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        messages = (
            self.session.snapshot_messages() if self.session is not None else list(self._messages)
        )
        meta = {
            "_meta": {
                "session_id": self.session_id,
                "role": None if shared_group else self.role.value,
                "assistant_mode": self.assistant_mode.value,
                "debug_mode": self.debug_mode,
                "code_model_selection": (
                    self.code_model_selection.to_payload()
                    if self.code_model_selection is not None
                    else None
                ),
                "code_model_once": (
                    self.code_model_once.to_payload() if self.code_model_once is not None else None
                ),
                "user_id": None if shared_group else self.workspace.user_id,
                "user_name": None if shared_group else self.workspace.user_name,
                "chat_kind": self.workspace.chat_kind,
                "chat_id": self.workspace.chat_id,
                "message_count": len(messages),
                "backend_session_ref": (None if shared_group else self._backend_session_payload()),
                "workspace_scope": self.workspace.scope,
                "execution_session_id": None if shared_group else self.execution_session_id,
                "turn_actor": self._turn_actor_payload(),
                "logged_at": now_iso,
            }
        }
        try:
            tmp = self._transcript_path.with_suffix(
                self._transcript_path.suffix + f".{uuid4().hex}.tmp"
            )
            with tmp.open("x", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
                for msg in messages:
                    line = dict(msg)
                    line["_logged_at"] = now_iso
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            tmp.chmod(0o600)
            tmp.replace(self._transcript_path)
        except OSError:
            _LOGGER.warning(
                "transcript 写入失败 path=%s (sid=%s)",
                self._transcript_path,
                self.session_id,
            )

    def _turn_actor_payload(self) -> dict[str, str | None] | None:
        identity = self.turn_identity
        if identity is None:
            return None
        payload = {
            "platform": identity.conversation.platform,
            "chat_kind": identity.conversation.chat_kind,
            "chat_id": identity.conversation.chat_id,
            "actor_ref": identity.actor_ref,
            "sender_user_name": identity.sender_user_name,
            "message_id": identity.message_id,
            "source": identity.source,
        }
        # Shared-group transcripts and the raw stable ID remain in protected
        # sibling state; model-visible attribution uses only the display ref.
        if self.workspace.scope != WORKSPACE_SCOPE_GROUP_SHARED:
            payload["sender_user_id"] = identity.sender_user_id
        return payload

    def _backend_session_payload(self) -> dict[str, str] | None:
        if self.session is None:
            return None
        ref = getattr(self.session, "backend_session_ref", None)
        if ref is None:
            return None
        return {
            "backend": str(getattr(ref, "backend", "")),
            "value": str(getattr(ref, "value", "")),
        }


def _make_test_session_state(
    *,
    session_id: str,
    workspace: Workspace,
    identity: str = "",
    role: Optional[Role] = None,
    assistant_mode: Optional[AssistantMode] = None,
) -> "SessionState":
    """测试用：构造一个最小可用的 SessionState（含一个不会调用 LLM 的 AgentSession）。

    handlers 默认抛 NotImplementedError；调用方按需 monkey-patch
    ``state.session.run_task`` 注入假实现。
    """
    from chatcopilot.agent.session import AgentSession
    from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder
    from chatcopilot.agent.tools.executor import ToolExecutor
    from chatcopilot.contracts.prompt import BotPromptProfile
    from types import SimpleNamespace

    profile = BotPromptProfile(
        identity=identity or "Test assistant",
        response_style="Return concise test responses.",
    )
    plan = PromptPlanBuilder().build(
        PromptBuildInput(
            profile=profile,
            backend="native",
            model=None,
            role=(role or Role.USER).value,
            channel_kind="group" if workspace.chat_kind == "group" else "private",
            session_policy="Test session policy.",
        )
    )

    fake_session = AgentSession(
        session_id=session_id,
        llm=None,  # type: ignore[arg-type]
        executor=ToolExecutor(tools=[]),
        tools_schema=[],
        prompt_plan=plan,
    )
    fake_session.capabilities = SimpleNamespace(  # type: ignore[attr-defined]
        tool_names=frozenset()
    )
    state = SessionState(
        session_id=session_id,
        workspace=workspace,
        role=role or Role.USER,
        assistant_mode=assistant_mode or AssistantMode.PERFORMANCE,
        runtime=SimpleNamespace(
            agent_backend="native",
            platform_type="test",
            prompt_profile=profile,
            capability_policies=(),
            skills=(),
            access=None,
        ),  # type: ignore[arg-type]
        session=None,
        llm_model=None,
        debug_mode=False,
    )
    state.materialize_workspace()
    state.attach_session(fake_session)
    return state


__all__ = ["SessionState", "_make_test_session_state"]
