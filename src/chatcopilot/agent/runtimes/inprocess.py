"""Explicit Native and LangGraph runtime adapters."""
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
from chatcopilot.contracts.runtime_adapter import (
    RuntimeCapabilities,
    RuntimeOpenRequest,
    RuntimeSessionRef,
    CAPABILITY_CHAT,
    CAPABILITY_REPOSITORY_MUTATION,
    CAPABILITY_TOOLS,
    require_runtime_capabilities,
)
from chatcopilot.contracts.cancellation import CancellationProbe
from chatcopilot.contracts.model_runtime import ResolvedRuntimeRoute


class _InProcessRuntimeAdapter:
    def __init__(
        self, runtime_id: str, *, tool_names: set[str],
        session_factory: Callable[[RuntimeOpenRequest], Any] | None = None,
    ) -> None:
        self.runtime_id = runtime_id
        self._session_factory = session_factory
        self._capabilities = RuntimeCapabilities(
            names=frozenset(
                {CAPABILITY_CHAT, CAPABILITY_TOOLS, CAPABILITY_REPOSITORY_MUTATION}
            ),
            tool_names=frozenset(tool_names),
        )
        self._sessions: dict[str, Any] = {}

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def open_session(self, request: RuntimeOpenRequest) -> RuntimeSessionRef:
        if request.route.runtime_id != self.runtime_id:
            raise ValueError("runtime route does not match adapter")
        require_runtime_capabilities(
            self.runtime_id, self.capabilities, request.required_capabilities
        )
        if self._session_factory is None:
            raise TypeError("in-process runtime requires a session_factory")
        value = f"{self.runtime_id}:{uuid.uuid4().hex}"
        self._sessions[value] = self._session_factory(request)
        return RuntimeSessionRef(self.runtime_id, value)

    def stream_turn(
        self,
        session: RuntimeSessionRef,
        task: AgentTask,
        *,
        on_event: EventSink,
        cancellation: CancellationProbe | None = None,
    ) -> AgentResult:
        if cancellation is None:
            return self._resolve(session).run_task(task, on_event=on_event)
        return self._resolve(session).run_task(
            task,
            on_event=on_event,
            cancellation=cancellation,
        )

    def close_session(self, session: RuntimeSessionRef) -> None:
        concrete = self._sessions.pop(session.value, None)
        close = getattr(concrete, "close", None)
        if callable(close):
            close()

    def cancel(self, session: RuntimeSessionRef) -> None:
        self._resolve(session).cancel()

    def discard_session(self, session: RuntimeSessionRef) -> None:
        self.close_session(session)

    def is_busy(self, session: RuntimeSessionRef) -> bool:
        return False  # Active turn ownership is held by RuntimeAgentSession.

    def _resolve(self, session: RuntimeSessionRef) -> Any:
        if session.runtime_id != self.runtime_id or session.value not in self._sessions:
            raise KeyError("unknown or cross-runtime session reference")
        return self._sessions[session.value]

    def current_session_ref(self, session: RuntimeSessionRef) -> RuntimeSessionRef:
        self._resolve(session)
        return session

    def set_prompt_plan(self, session: RuntimeSessionRef, plan: Any) -> None:
        self._resolve(session).set_prompt_plan(plan)

    def record_exchange(
        self, session: RuntimeSessionRef, user_text: str, assistant_text: str
    ) -> None:
        self._resolve(session).record_exchange(user_text, assistant_text)

    def snapshot_messages(self, session: RuntimeSessionRef) -> list[dict[str, Any]]:
        return self._resolve(session).snapshot_messages()

    def snapshot_transcript(self, session: RuntimeSessionRef):
        return self._resolve(session).snapshot_transcript()


def _build_inprocess_runtime_adapter(
    adapter_type: type[_InProcessRuntimeAdapter], session_cls: type[AgentSession], *,
    route: ResolvedRuntimeRoute,
    tool_names: set[str], llm: LLMClient | None = None,
    runtime_config: ChatConfig | None = None,
    tool_executor: ToolExecutor | None = None,
    tools_schema: list[dict[str, Any]] | None = None,
    tool_payload_filter: ToolPayloadFilter | None = None,
    retriever: Retriever | None = None,
    **_: Any,
) -> _InProcessRuntimeAdapter:
    expected_runtime_id = "native" if adapter_type is NativeRuntimeAdapter else "langgraph"
    if route.runtime_id != expected_runtime_id:
        raise ValueError("runtime route does not match in-process adapter")
    def create_session(request: RuntimeOpenRequest) -> AgentSession:
        if llm is None or runtime_config is None or tool_executor is None:
            raise TypeError("in-process runtime requires model, config and executor")

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
            hard_iteration_cap=getattr(rt, "hard_iteration_cap", _defaults_rt.hard_iteration_cap),
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

    return adapter_type(
        tool_names=tool_names, session_factory=create_session,
    )


class NativeRuntimeAdapter(_InProcessRuntimeAdapter):
    def __init__(self, *, tool_names, session_factory=None):
        super().__init__("native", tool_names=tool_names, session_factory=session_factory)


class LangGraphRuntimeAdapter(_InProcessRuntimeAdapter):
    def __init__(self, *, tool_names, session_factory=None):
        super().__init__("langgraph", tool_names=tool_names, session_factory=session_factory)


def build_native_runtime_adapter(**kwargs) -> NativeRuntimeAdapter:
    return _build_inprocess_runtime_adapter(NativeRuntimeAdapter, AgentSession, **kwargs)


def build_langgraph_runtime_adapter(**kwargs) -> LangGraphRuntimeAdapter:
    from chatcopilot.agent.langgraph_session import LangGraphAgentSession
    return _build_inprocess_runtime_adapter(LangGraphRuntimeAdapter, LangGraphAgentSession, **kwargs)


__all__ = [
    "NativeRuntimeAdapter",
    "LangGraphRuntimeAdapter",
    "build_native_runtime_adapter",
    "build_langgraph_runtime_adapter",
]
