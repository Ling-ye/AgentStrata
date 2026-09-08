"""Native and LangGraph backend adapters."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, cast

from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.topic import TopicLlm, TopicPolicy, TopicRelevanceClassifier
from chatcopilot.agent.rag.provider import Retriever
from chatcopilot.agent.session import AgentSession, ToolPayloadFilter
from chatcopilot.agent.tools.executor import ToolExecutor
from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import LLMClient

from chatcopilot.contracts.agent import AgentResult, AgentTask, EventSink
from chatcopilot.contracts.agent_backend import (
    BackendCapabilities,
    BackendOpenRequest,
    BackendSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_REPOSITORY_MUTATION,
    CAPABILITY_TOOLS,
    require_backend_capabilities,
)
from chatcopilot.contracts.cancellation import CancellationProbe


class InProcessAgentBackend:
    def __init__(
        self, backend_id: str, *, tool_names: set[str],
        session_factory: Callable[[BackendOpenRequest], Any] | None = None,
    ) -> None:
        self.backend_id = backend_id
        self._session_factory = session_factory
        self._capabilities = BackendCapabilities(
            names=frozenset(
                {CAPABILITY_CHAT, CAPABILITY_TOOLS, CAPABILITY_REPOSITORY_MUTATION}
            ),
            tool_names=frozenset(tool_names),
        )
        self._sessions: dict[str, Any] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def open_session(self, request: BackendOpenRequest) -> BackendSessionRef:
        require_backend_capabilities(
            self.backend_id, self.capabilities, request.required_capabilities
        )
        if self._session_factory is None:
            raise TypeError("in-process backend requires a session_factory")
        value = f"{self.backend_id}:{uuid.uuid4().hex}"
        self._sessions[value] = self._session_factory(request)
        return BackendSessionRef(self.backend_id, value)

    def stream_turn(
        self,
        session: BackendSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        if cancellation is None:
            return self.native_session(session).run_task(task, on_event=on_event)
        return self.native_session(session).run_task(
            task,
            on_event=on_event,
            cancellation=cancellation,
        )

    def close_session(self, session: BackendSessionRef) -> None:
        concrete = self._sessions.pop(session.value, None)
        close = getattr(concrete, "close", None)
        if callable(close):
            close()

    def native_session(self, session: BackendSessionRef) -> Any:
        if session.backend != self.backend_id or session.value not in self._sessions:
            raise KeyError("unknown or cross-backend session reference")
        return self._sessions[session.value]

    def current_session_ref(self, session: BackendSessionRef) -> BackendSessionRef:
        self.native_session(session)
        return session

    def set_prompt_plan(self, session: BackendSessionRef, plan: Any) -> None:
        self.native_session(session).set_prompt_plan(plan)

    def record_exchange(
        self, session: BackendSessionRef, user_text: str, assistant_text: str
    ) -> None:
        self.native_session(session).record_exchange(user_text, assistant_text)

    def snapshot_messages(self, session: BackendSessionRef) -> list[dict[str, Any]]:
        return self.native_session(session).snapshot_messages()


def build_inprocess_backend(
    backend_id: str, *, tool_names: set[str], llm: LLMClient | None = None,
    runtime_config: ChatConfig | None = None,
    tool_executor: ToolExecutor | None = None,
    tools_schema: list[dict[str, Any]] | None = None,
    tool_payload_filter: ToolPayloadFilter | None = None,
    retriever: Retriever | None = None,
    **_: Any,
) -> InProcessAgentBackend:
    def create_session(request: BackendOpenRequest) -> AgentSession:
        if llm is None or runtime_config is None or tool_executor is None:
            raise TypeError("in-process backend requires model, config and executor")
        session_cls: type[AgentSession]
        if backend_id == "langgraph":
            from chatcopilot.agent.langgraph_session import LangGraphAgentSession

            session_cls = LangGraphAgentSession
        else:
            session_cls = AgentSession

        rt = runtime_config.runtime
        _defaults = ContextManager()
        ctx_mgr = ContextManager(
            max_context_tokens=getattr(rt, "max_context_tokens", _defaults.max_context_tokens),
            sliding_window_turns=getattr(rt, "sliding_window_turns", _defaults.sliding_window_turns),
            tool_result_summary_max_tokens=getattr(
                rt, "tool_result_summary_max_tokens", _defaults.tool_result_summary_max_tokens
            ),
        )
        topic_policy = TopicPolicy(
            enabled=bool(getattr(rt, "topic_classifier_enabled", False)),
            mode=getattr(rt, "topic_classifier_mode", "off"),
            model=getattr(rt, "topic_model", "") or None,
            uncertain_mode=getattr(rt, "topic_uncertain_mode", "continue"),
            related_threshold=getattr(rt, "topic_related_threshold", 0.70),
            unrelated_threshold=getattr(rt, "topic_unrelated_threshold", 0.75),
            current_max_chars=getattr(rt, "topic_current_max_chars", 1200),
            previous_user_max_chars=getattr(rt, "topic_previous_user_max_chars", 800),
            previous_assistant_max_chars=getattr(rt, "topic_previous_assistant_max_chars", 800),
            decision_cache_size=getattr(rt, "topic_decision_cache_size", 256),
            decision_cache_ttl_seconds=getattr(rt, "topic_decision_cache_ttl_seconds", 300),
        )
        topic_classifier = (
            TopicRelevanceClassifier(cast(TopicLlm, llm), topic_policy)
            if topic_policy.active
            else None
        )

        _defaults_rt = ChatConfig().runtime
        return session_cls(
            session_id=request.session_id,
            llm=llm,
            executor=tool_executor,
            tools_schema=tools_schema or [],
            prompt_plan=request.prompt_plan,
            tool_payload_filter=tool_payload_filter,
            context_manager=ctx_mgr,
            topic_classifier=topic_classifier,
            max_tool_iterations=max(
                1,
                getattr(
                    rt,
                    "max_tool_iterations",
                    _defaults_rt.max_tool_iterations,
                ),
            ),
            hard_iteration_cap=max(
                1,
                getattr(
                    rt,
                    "hard_iteration_cap",
                    _defaults_rt.hard_iteration_cap,
                ),
            ),
            max_tool_calls=getattr(
                rt,
                "max_tool_calls",
                _defaults_rt.max_tool_calls,
            ),
            timeout_seconds=getattr(
                rt,
                "turn_timeout_seconds",
                _defaults_rt.turn_timeout_seconds,
            ),
            hard_timeout_seconds=getattr(
                rt,
                "hard_timeout_seconds",
                _defaults_rt.hard_timeout_seconds,
            ),
            stall_window_seconds=max(
                10,
                getattr(
                    rt,
                    "stall_window_seconds",
                    _defaults_rt.stall_window_seconds,
                ),
            ),
            max_consecutive_tool_failures=max(1, rt.max_tool_retries),
            retriever=retriever,
        )

    return InProcessAgentBackend(
        backend_id, tool_names=tool_names, session_factory=create_session,
    )


__all__ = ["InProcessAgentBackend", "build_inprocess_backend"]
