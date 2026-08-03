"""ACP 单个会话的状态容器。

middleware/acp 内部用 ``SessionState`` 承载一次 ACP session 的全部上下文：
``Workspace`` + ``Role`` + ``AgentSession`` + 当前业务模式/调试模式 + bot runtime
快照（用于重建 system prompt）。这样 ACP server 的各个 handler 不直接持有
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

from chatcopilot.agent.session_protocol import AgentSessionProtocol
from chatcopilot.botspec import BotRuntimeContext
from chatcopilot.contracts.agent import ResourceRef
from chatcopilot.contracts.model_selection import (
    CodeModelSelection,
    MODEL_SELECTION_SCOPE_ONCE,
    MODEL_SELECTION_SCOPE_SESSION,
)
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.middleware.access_control import AssistantMode, Role
from chatcopilot.middleware.runtime.workspace import Workspace

if TYPE_CHECKING:
    from chatcopilot.core.config import RoutingConfig

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.session_state")


def _sanitize_session_id(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_.@") else "_" for ch in value).strip("_") or "session"


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
    code_model_selection: CodeModelSelection | None = None
    code_model_once: CodeModelSelection | None = None
    debug_mode: bool = False
    pending_image_resources: tuple[ResourceRef, ...] = field(
        default=(),
        repr=False,
    )
    pending_image_names: tuple[str, ...] = field(default=(), repr=False)
    _transcript_path: Optional[Path] = field(default=None, repr=False)
    _pending_exchanges: list[tuple[str, str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self._transcript_path is None:
            try:
                self.workspace.transcripts.mkdir(parents=True, exist_ok=True)
                start_iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                fname = f"{_sanitize_session_id(self.session_id)}__{start_iso}.jsonl"
                self._transcript_path = self.workspace.transcripts / fname
            except OSError:
                _LOGGER.exception("无法初始化 transcript 路径，本会话不落盘")
                self._transcript_path = None

    # ------------------------------------------------------------------
    # BotRuntimeContext 字段的便捷代理（重建 system prompt 时用）
    # 测试场景下 ``runtime`` 可能为 None，统一退化为安全默认值。
    # ------------------------------------------------------------------
    @property
    def bot_system_prompt(self) -> str:
        return getattr(self.runtime, "system_prompt", "") if self.runtime is not None else ""

    @property
    def bot_refusal_prompt(self) -> Optional[str]:
        return getattr(self.runtime, "refusal_prompt", None) if self.runtime is not None else None

    @property
    def safety_prompt_override(self) -> Optional[str]:
        return getattr(self.runtime, "safety_prompt_override", None) if self.runtime is not None else None

    @property
    def memory_prompt_override(self) -> Optional[str]:
        return getattr(self.runtime, "memory_prompt_override", None) if self.runtime is not None else None

    @property
    def mode_prompt_overrides(self) -> dict[str, str]:
        if self.runtime is None:
            return {}
        return getattr(self.runtime, "mode_prompt_overrides", {})

    @property
    def role_prompt_overrides(self) -> dict[str, str]:
        if self.runtime is None:
            return {}
        return getattr(self.runtime, "role_prompt_overrides", {})

    @property
    def capability_prompt_fragments(self) -> tuple[str, ...]:
        if self.runtime is None:
            return ()
        return getattr(self.runtime, "capability_prompt_fragments", ())

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
        if self.session is None:
            self._pending_exchanges.append((user_text, assistant_text))
        else:
            self.session.record_exchange(user_text, assistant_text)
        self.persist_transcript()

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
        self.persist_transcript()

    def set_assistant_mode(self, mode: AssistantMode, system_prompt: str, *, session_dynamic_tail: str = "") -> None:
        """切换业务模式：同步更新本 state + AgentSession 的 system baseline。

        ``session_dynamic_tail`` 仅在 refresh 路径使用，追加到 baseline 末尾再经
        renderer 处理。初始 session 创建时 persona 通过 renderer 闭包中独立的
        ``session_dynamic_tail`` 参数控制位置。
        """
        self.assistant_mode = mode
        effective = system_prompt
        tail = (session_dynamic_tail or "").strip()
        if tail:
            base = (system_prompt or "").strip()
            effective = f"{base}\n\n{tail}" if base else tail
        if self.session is not None:
            self.session.set_system_baseline(effective)

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
        if self._transcript_path is None:
            return
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        messages = (
            self.session.snapshot_messages()
            if self.session is not None
            else list(self._messages)
        )
        meta = {
            "_meta": {
                "session_id": self.session_id,
                "role": self.role.value,
                "assistant_mode": self.assistant_mode.value,
                "debug_mode": self.debug_mode,
                "code_model_selection": (
                    self.code_model_selection.to_payload()
                    if self.code_model_selection is not None
                    else None
                ),
                "code_model_once": (
                    self.code_model_once.to_payload()
                    if self.code_model_once is not None
                    else None
                ),
                "user_id": self.workspace.user_id,
                "user_name": self.workspace.user_name,
                "chat_kind": self.workspace.chat_kind,
                "chat_id": self.workspace.chat_id,
                "message_count": len(messages),
                "backend_session_ref": self._backend_session_payload(),
                "logged_at": now_iso,
            }
        }
        try:
            tmp = self._transcript_path.with_suffix(self._transcript_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
                for msg in messages:
                    line = dict(msg)
                    line["_logged_at"] = now_iso
                    fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            tmp.replace(self._transcript_path)
        except OSError:
            _LOGGER.warning(
                "transcript 写入失败 path=%s (sid=%s)",
                self._transcript_path,
                self.session_id,
            )

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
    system_prompt: str = "",
    role: Optional[Role] = None,
    assistant_mode: Optional[AssistantMode] = None,
) -> "SessionState":
    """测试用：构造一个最小可用的 SessionState（含一个不会调用 LLM 的 AgentSession）。

    handlers 默认抛 NotImplementedError；调用方按需 monkey-patch
    ``state.session.run_task`` 注入假实现。
    """
    from chatcopilot.agent.session import AgentSession
    from chatcopilot.agent.tools.executor import ToolExecutor

    fake_session = AgentSession(
        session_id=session_id,
        llm=None,  # type: ignore[arg-type]
        executor=ToolExecutor(tools=[]),
        tools_schema=[],
        system_baseline=system_prompt,
    )
    return SessionState(
        session_id=session_id,
        workspace=workspace,
        role=role or Role.USER,
        assistant_mode=assistant_mode or AssistantMode.PERFORMANCE,
        runtime=None,  # type: ignore[arg-type]
        session=fake_session,
        llm_model=None,
        debug_mode=False,
    )


__all__ = ["SessionState", "_make_test_session_state"]
