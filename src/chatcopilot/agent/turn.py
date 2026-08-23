"""Shared per-turn runtime operations for agent backends.

Backends decide control flow: native uses a Python loop, LangGraph uses a
``StateGraph``.  This module owns the common Agent-layer semantics that should
not vary by backend: task framing, ``AgentEvent`` emission, tool-result
messages, lifecycle intents, produced resources, and ``AgentResult`` assembly.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from chatcopilot.agent.context import (
    frame_task_content,
    frame_task_message,
    validated_image_resource_receipts,
)
from chatcopilot.agent.context.token_estimator import (
    estimate_prompt_tokens,
)
from chatcopilot.agent.context.topic import TopicDecision
from chatcopilot.agent.lifecycle import (
    reset_lifecycle_intent_collector,
    set_lifecycle_intent_collector,
)
from chatcopilot.core.llm_client import ChatResult
from chatcopilot.core.observability_redaction import (
    omit_local_resource_paths,
    omit_private_reasoning_messages,
)
from chatcopilot.contracts.agent import (
    AgentResult,
    AgentStopReason,
    AgentTask,
    ContextSnapshotPrepared,
    DeferredLifecycleIntent,
    EventSink,
    FinalText,
    InputResourcesDispatched,
    LlmCallStarted,
    LlmCallFinished,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TopicDecisionMade,
    TurnError,
)
from chatcopilot.agent.response_integrity import ResponseIntegrityResult
from chatcopilot.agent.turn_support import (
    DEV_WRITE_TOOLS as _DEV_WRITE_TOOLS,
    EMPTY_MODEL_REPLY_TEXT as _EMPTY_MODEL_REPLY_TEXT,
    FINALIZE_SELF_UPDATE_TOOL as _FINALIZE_SELF_UPDATE_TOOL,
    SEARCH_INFORMATION_TOOL as _SEARCH_INFORMATION_TOOL,
    SELF_UPDATE_FINAL_TOOL_NAMES as _SELF_UPDATE_FINAL_TOOL_NAMES,
    SELF_UPDATE_REQUIRED_PROMPT as _SELF_UPDATE_REQUIRED_PROMPT,
    paths_to_resources as _paths_to_resources,
    primary_artifact_kind as _primary_artifact_kind,
    repeated_search_result as _repeated_search_result,
    safe_emit as _safe_emit,
    task_trace_id as _task_trace_id,
    tool_fingerprint as _tool_fingerprint,
)
from chatcopilot.contracts.tools import ToolResult
from chatcopilot.agent.trace import (
    TraceContext,
    new_span_id,
    new_trace_id,
    reset_trace,
    set_trace,
)

_LOGGER = logging.getLogger("chatcopilot.agent.turn")


@dataclass
class TurnState:
    """Mutable state for one AgentTask run.

    The state intentionally contains only Agent-layer values.  Platform
    delivery remains in middleware, and backend-specific execution details stay
    in the backend session.
    """

    messages: list[dict[str, Any]]
    started_at: float
    trace_id: str
    root_span: str
    context_kind: str = "sliding_window"
    topic_decision: TopicDecision | None = None
    llm_view: list[dict[str, Any]] | None = None
    final_text: str = ""
    stop_reason: AgentStopReason = "end_turn"
    consecutive_failures: int = 0
    tool_calls_used: int = 0
    produced_paths: list[tuple[str, str]] = field(default_factory=list)
    last_successful_tool_summary: str = ""
    last_successful_search_summary: str = ""
    recent_tool_fingerprints: list[str] = field(default_factory=list)
    successful_operations: list[str] = field(default_factory=list)
    lifecycle_intents: list[DeferredLifecycleIntent] = field(default_factory=list)
    self_update_required: bool = False
    iteration: int = 0
    done: bool = False
    response_integrity: ResponseIntegrityResult | None = None
    last_tool_finish_time: float = 0.0
    wrapup_injected: bool = False
    wrapup_remaining: int = 0


@dataclass
class TurnOps:
    """Common operations used by Agent backends during one turn."""

    session: Any
    task: AgentTask
    on_event: EventSink

    def initial_state(self) -> TurnState:
        user_text = frame_task_message(self.task)
        context = (self.task.turn_context or "").strip()
        if context:
            user_text = f"{user_text}\n\n{context}".strip()
        rag_snippet = self.session._retrieve_context(self.task.text)
        if rag_snippet:
            user_text = f"{user_text}\n\n{rag_snippet}".strip()
        user_content = frame_task_content(self.task, text=user_text)
        self.session._messages.append({"role": "user", "content": user_content})

        trace_id = self.session.trace_id or _task_trace_id(self.task) or new_trace_id()
        root_span = self.session.trace_parent_span_id or new_span_id()
        started_at = time.monotonic()
        state = TurnState(
            messages=self.session._messages,
            started_at=started_at,
            trace_id=trace_id,
            root_span=root_span,
            last_tool_finish_time=started_at,
        )

        if self.session.topic_classifier is not None:
            routing_started_at = time.time()
            topic_messages = _text_only_messages(
                self.session._messages,
                prompt_prefix_length=self.session.prompt_prefix_length,
            )
            decision = self.session.topic_classifier.classify(
                messages=topic_messages,
                current_user_text=user_text,
                metadata=self.task.metadata,
            )
            routing_finished_at = time.time()
            state.topic_decision = decision
            state.context_kind = decision.context_kind or state.context_kind
            self.emit(
                TopicDecisionMade(
                    decision=decision.kind,
                    context_kind=decision.context_kind,
                    confidence=decision.confidence,
                    reason=decision.reason,
                    source=decision.source,
                    model=decision.model,
                    usage=decision.usage,
                    started_at=routing_started_at,
                    finished_at=routing_finished_at,
                    elapsed_s=round(routing_finished_at - routing_started_at, 4),
                )
            )

        return state

    def emit(self, event: Any) -> None:
        _safe_emit(self.on_event, event)

    def should_stop_before_llm(self, state: TurnState) -> bool:
        if state.done:
            return True
        if self.session._hard_timed_out(state.started_at) or self.session._soft_timed_out(
            state.started_at
        ):
            self.finish_timeout(state, hard=self.session._hard_timed_out(state.started_at))
            return True
        if state.iteration >= self.session.hard_iteration_cap:
            self.session._repair_orphan_tool_calls(self.session._messages)
            text = (
                f"（已达迭代硬上限 {self.session.hard_iteration_cap} 轮，无条件停止。"
                "如有需要请追问以继续。）"
            )
            self.finish_text(state, text, stop_reason="iteration_cap")
            return True
        return False

    def call_llm(self, state: TurnState) -> ChatResult | None:
        call_messages = self._build_llm_call_messages(state)
        self.session._repair_orphan_tool_calls(call_messages)
        iteration = state.iteration
        model = getattr(self.session.llm, "model", "")
        call_span_id = new_span_id()
        image_receipts = validated_image_resource_receipts(self.task)
        prompt_estimate = estimate_prompt_tokens(call_messages, self.session.tools_schema)
        snapshot_id = f"ctx_{call_span_id}"
        safe_session = omit_private_reasoning_messages(self.session._messages)
        safe_effective = omit_private_reasoning_messages(call_messages)
        path_safe_session = omit_local_resource_paths(safe_session.messages)
        path_safe_effective = omit_local_resource_paths(safe_effective.messages)
        reasoning_omission_count = safe_session.omission_count + safe_effective.omission_count
        resource_path_omission_count = (
            path_safe_session.omission_count + path_safe_effective.omission_count
        )
        omitted: list[str] = []
        if len(call_messages) < len(self.session._messages):
            omitted.append("context_window_messages_excluded")
        if call_messages != self.session._messages:
            omitted.append("context_view_transformed")
        if image_receipts:
            omitted.append("binary_resource_payload_not_persisted")
        if reasoning_omission_count:
            omitted.append("provider_private_reasoning")
        if resource_path_omission_count:
            omitted.append("local_resource_paths")
        partial_capture = bool(
            image_receipts or reasoning_omission_count or resource_path_omission_count
        )
        self.emit(
            ContextSnapshotPrepared(
                snapshot_id=snapshot_id,
                backend=str(getattr(self.session, "backend_name", "native")),
                model=model,
                iteration=iteration,
                session_messages=path_safe_session.messages,
                effective_messages=path_safe_effective.messages,
                tool_schemas=tuple(copy.deepcopy(self.session.tools_schema)),
                resources=image_receipts,
                coverage="partial" if partial_capture else "exact_model_input",
                omitted=tuple(omitted),
                context_kind=state.context_kind,
                trace_id=state.trace_id,
                span_id=call_span_id,
                parent_span_id=state.root_span,
                depth=self.session.trace_depth,
                estimated_tokens=int(prompt_estimate["tokens"]),
                model_selection={"model": model},
                private_reasoning_omission_count=reasoning_omission_count,
                resource_path_omission_count=resource_path_omission_count,
            )
        )
        self.emit(
            LlmCallStarted(
                model=model,
                iteration=iteration,
                backend=str(getattr(self.session, "backend_name", "native")),
                trace_id=state.trace_id,
                span_id=call_span_id,
                parent_span_id=state.root_span,
                depth=self.session.trace_depth,
                input_message_count=len(call_messages),
                input_estimated_tokens=int(prompt_estimate["tokens"]),
                system_estimated_tokens=int(prompt_estimate["system_tokens"]),
                tool_schema_count=len(self.session.tools_schema),
                tool_schema_estimated_tokens=int(prompt_estimate["tool_schema_tokens"]),
                estimator_version=str(prompt_estimate["estimator_version"]),
                context_kind=state.context_kind,
                context_snapshot_id=snapshot_id,
            )
        )
        try:
            result = self.session.llm.chat(
                messages=call_messages,
                tools=self.session.tools_schema,
                stream=self.session.stream_first_turn and iteration == 0,
                on_content_delta=(
                    self._wrap_text_delta()
                    if self.session.stream_first_turn and iteration == 0
                    else None
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("LLM 调用失败")
            err_text = f"（与模型通信失败：{type(exc).__name__}: {exc}；请稍后再试）"
            self.emit(
                LlmCallFinished(
                    model=model,
                    iteration=iteration,
                    backend=str(getattr(self.session, "backend_name", "native")),
                    finish_reason="failed",
                    usage=None,
                    ok=False,
                    trace_id=state.trace_id,
                    span_id=call_span_id,
                    parent_span_id=state.root_span,
                    depth=self.session.trace_depth,
                    input_message_count=len(call_messages),
                    input_estimated_tokens=int(prompt_estimate["tokens"]),
                    system_estimated_tokens=int(prompt_estimate["system_tokens"]),
                    tool_schema_count=len(self.session.tools_schema),
                    tool_schema_estimated_tokens=int(prompt_estimate["tool_schema_tokens"]),
                    estimator_version=str(prompt_estimate["estimator_version"]),
                    context_kind=state.context_kind,
                    context_snapshot_id=snapshot_id,
                )
            )
            self.emit(TurnError(code=type(exc).__name__, message=str(exc)))
            self.finish_text(state, err_text, stop_reason="llm_error")
            return None

        if image_receipts:
            raw_turn = self.task.metadata.get("eval_turn", 0)
            turn_index = raw_turn if isinstance(raw_turn, int) and raw_turn >= 0 else 0
            self.emit(
                InputResourcesDispatched(
                    backend=str(getattr(self.session, "backend_name", "native")),
                    turn_index=turn_index,
                    request_id=call_span_id,
                    resources=image_receipts,
                )
            )

        self.emit(
            LlmCallFinished(
                model=model,
                iteration=iteration,
                backend=str(getattr(self.session, "backend_name", "native")),
                finish_reason=result.finish_reason,
                usage=result.usage,
                trace_id=state.trace_id,
                span_id=call_span_id,
                parent_span_id=state.root_span,
                depth=self.session.trace_depth,
                input_message_count=len(call_messages),
                input_estimated_tokens=int(prompt_estimate["tokens"]),
                system_estimated_tokens=int(prompt_estimate["system_tokens"]),
                tool_schema_count=len(self.session.tools_schema),
                tool_schema_estimated_tokens=int(prompt_estimate["tool_schema_tokens"]),
                estimator_version=str(prompt_estimate["estimator_version"]),
                context_kind=state.context_kind,
                context_snapshot_id=snapshot_id,
            )
        )

        assistant_msg = result.to_message()
        self.session._messages.append(assistant_msg)
        if state.llm_view is not None and state.llm_view is not self.session._messages:
            state.llm_view.append(assistant_msg)
        state.messages = self.session._messages
        state.iteration = iteration + 1

        blocked_self_update_final = state.self_update_required and not result.tool_calls
        if result.content and not blocked_self_update_final:
            self.emit(FinalText(text=result.content))
            state.final_text = result.content

        if not result.tool_calls:
            if state.self_update_required:
                self.append_user_instruction(state, _SELF_UPDATE_REQUIRED_PROMPT)
            else:
                self.finish_without_tool_result(state, result_content=result.content)
        return result

    def last_assistant_tool_calls(self) -> list[dict[str, Any]]:
        last = self.session._messages[-1] if self.session._messages else {}
        calls = last.get("tool_calls") if isinstance(last, dict) else None
        return list(calls or [])

    def execute_tool_call(self, state: TurnState, tool_call: dict[str, Any]) -> None:
        if (
            self.session.max_tool_calls is not None
            and state.tool_calls_used >= self.session.max_tool_calls
        ):
            self.finish_tool_call_cap(state)
            return

        name, args = self.session._parse_tool_call(tool_call)
        span_id = new_span_id()
        self.emit(
            ToolStarted(
                name=name,
                arguments=args,
                trace_id=state.trace_id,
                span_id=span_id,
                parent_span_id=state.root_span,
                depth=self.session.trace_depth,
            )
        )

        tool_result = self._run_tool(state, name, args, span_id)
        state.tool_calls_used += 1
        state.last_tool_finish_time = time.monotonic()
        state.recent_tool_fingerprints.append(_tool_fingerprint(name, args))
        self._append_tool_message(state, tool_call, name, tool_result)

        self.emit(
            ToolFinished(
                name=name,
                ok=tool_result.ok,
                summary=tool_result.summary,
                error=None if tool_result.ok else (tool_result.error or "工具执行失败"),
                trace_id=state.trace_id,
                span_id=span_id,
                parent_span_id=state.root_span,
                depth=self.session.trace_depth,
                data=tool_result.to_llm_payload(),
            )
        )

        committed = tool_result.data.get("committed")
        if committed is True or (tool_result.ok and committed is not False):
            state.successful_operations.append(name)

        if not tool_result.ok:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.session.max_consecutive_tool_failures:
                fail_text = (
                    f"（连续 {state.consecutive_failures} 次工具失败，已停止自动重试；"
                    "请检查参数后再试或换一种问法）"
                )
                self.finish_text(state, fail_text, stop_reason="tool_failure_cap")
            return

        state.consecutive_failures = 0
        if name in _DEV_WRITE_TOOLS:
            state.self_update_required = True
        elif name == _FINALIZE_SELF_UPDATE_TOOL:
            state.self_update_required = False
        if tool_result.summary:
            state.last_successful_tool_summary = tool_result.summary
            if name == _SEARCH_INFORMATION_TOOL:
                state.last_successful_search_summary = tool_result.summary
        artifact_kind = _primary_artifact_kind(tool_result.artifact_kinds)
        if artifact_kind:
            for output in tool_result.outputs or []:
                item = (output, artifact_kind) if isinstance(output, str) else None
                if item is not None and item not in state.produced_paths:
                    state.produced_paths.append(item)

    def finish_without_tool_result(self, state: TurnState, *, result_content: str) -> None:
        final_text = state.final_text or result_content
        if not final_text and state.last_successful_tool_summary:
            final_text = state.last_successful_tool_summary
            self.emit(FinalText(text=final_text))
            self._patch_last_assistant_content(final_text, state)
        elif not final_text:
            final_text = _EMPTY_MODEL_REPLY_TEXT
            self.emit(FinalText(text=final_text))
            self._patch_last_assistant_content(final_text, state)
            _LOGGER.warning(
                "LLM returned empty assistant reply without tool calls | sid=%s",
                self.session.session_id,
            )
        state.final_text = final_text
        state.response_integrity = self.session._run_response_integrity(
            final_text,
            successful_operations=tuple(state.successful_operations),
        )
        if any(issue.startswith("missing_receipt:") for issue in state.response_integrity.issues):
            final_text = "未能确认该操作已完成：本轮缺少相应的可信成功回执。"
            self.emit(FinalText(text=final_text))
            self._patch_last_assistant_content(final_text, state)
            state.final_text = final_text
        state.done = True

    def finish_timeout(self, state: TurnState, *, hard: bool = False) -> None:
        limit = self.session.hard_timeout_seconds if hard else self.session.timeout_seconds
        self.session._repair_orphan_tool_calls(self.session._messages)
        text = f"（已达执行超时{'硬' if hard else ''}上限 {limit} 秒，停止本轮。）"
        self.finish_text(state, text, stop_reason="timeout_cap")

    def finish_tool_call_cap(self, state: TurnState) -> None:
        limit = self.session.max_tool_calls or 0
        self.session._repair_orphan_tool_calls(self.session._messages)
        self.finish_text(
            state,
            self.session._tool_call_cap_text(limit),
            stop_reason="tool_call_cap",
        )

    def finish_text(
        self,
        state: TurnState,
        text: str,
        *,
        stop_reason: AgentStopReason,
    ) -> None:
        self.emit(FinalText(text=text))
        self.session._messages.append({"role": "assistant", "content": text})
        if state.llm_view is not None and state.llm_view is not self.session._messages:
            state.llm_view.append({"role": "assistant", "content": text})
        state.messages = self.session._messages
        state.final_text = text
        state.stop_reason = stop_reason
        state.done = True

    def append_user_instruction(self, state: TurnState, text: str) -> None:
        message = {"role": "user", "content": text}
        self.session._messages.append(message)
        if state.llm_view is not None and state.llm_view is not self.session._messages:
            state.llm_view.append(message)
        state.messages = self.session._messages

    def result_from_state(self, state: TurnState) -> AgentResult:
        final_text = state.final_text or _EMPTY_MODEL_REPLY_TEXT
        return AgentResult(
            final_text=final_text,
            stop_reason=state.stop_reason,
            produced_resources=_paths_to_resources(state.produced_paths),
            message_count=len(self.session._messages),
            response_integrity=state.response_integrity,
            lifecycle_intents=tuple(state.lifecycle_intents),
        )

    def _build_llm_call_messages(self, state: TurnState) -> list[dict[str, Any]]:
        if state.iteration == 0 and self.session.context_manager is not None:
            state.llm_view = self.session.context_manager.prepare_messages(
                self.session._messages,
                topic_decision=state.topic_decision,
                prompt_prefix_length=self.session.prompt_prefix_length,
            )
        elif state.llm_view is None:
            state.llm_view = self.session._messages
        return self.session._maybe_inject_budget_warning(
            state.llm_view,
            iteration=state.iteration,
            tool_calls_used=state.tool_calls_used,
            wrapup=state.wrapup_injected,
        )

    def _wrap_text_delta(self):
        def _delta(text: str) -> None:
            if text:
                self.emit(TextDelta(text=text))

        return _delta

    def _run_tool(
        self,
        state: TurnState,
        name: str,
        args: dict[str, Any],
        span_id: str,
    ) -> ToolResult:
        if name == _SEARCH_INFORMATION_TOOL and state.last_successful_search_summary:
            return _repeated_search_result(state.last_successful_search_summary)
        if state.self_update_required and name in _SELF_UPDATE_FINAL_TOOL_NAMES:
            return ToolResult(
                ok=False,
                summary="",
                outputs=[],
                console="",
                doc_links=[],
                error=(
                    "self_update_required: call finalize_self_update successfully "
                    "before submitting the final result"
                ),
            )
        if name == _FINALIZE_SELF_UPDATE_TOOL:
            return self._register_finalize_intent(state, args)

        bubbled: list[DeferredLifecycleIntent] = []

        def collect(intent: DeferredLifecycleIntent) -> None:
            if state.lifecycle_intents or bubbled:
                raise RuntimeError("only one deferred lifecycle intent is allowed per turn")
            bubbled.append(intent)

        trace_token = set_trace(
            TraceContext(
                trace_id=state.trace_id,
                span_id=span_id,
                depth=self.session.trace_depth,
                sink=self.on_event,
            )
        )
        lifecycle_token = set_lifecycle_intent_collector(collect)
        try:
            result = self.session.executor.execute(
                name,
                args,
                request_text=self.task.text,
            )
        finally:
            reset_lifecycle_intent_collector(lifecycle_token)
            reset_trace(trace_token)
        if result.ok and bubbled:
            state.lifecycle_intents.extend(bubbled)
        return result

    def _register_finalize_intent(
        self,
        state: TurnState,
        args: dict[str, Any],
    ) -> ToolResult:
        if not (state.final_text or "").strip():
            return ToolResult(
                ok=False,
                summary="",
                outputs=[],
                console="",
                doc_links=[],
                error=(
                    "finalize_self_update requires a non-empty user-facing final summary "
                    "before registering the deferred lifecycle intent"
                ),
            )
        if state.lifecycle_intents:
            return ToolResult(
                ok=False,
                summary="",
                outputs=[],
                console="",
                doc_links=[],
                error="only one deferred lifecycle intent is allowed per turn",
            )
        state.lifecycle_intents.append(
            DeferredLifecycleIntent(
                name="finalize_self_update",
                arguments=dict(args),
                source="subagent" if self.session.trace_depth > 0 else "main",
            )
        )
        return ToolResult(
            ok=True,
            summary="已登记延迟自更新；最终回复完成 ACP 投递后才会启动重启。",
            outputs=[],
            console="",
            doc_links=[],
        )

    def _append_tool_message(
        self,
        state: TurnState,
        tool_call: dict[str, Any],
        name: str,
        tool_result: ToolResult,
    ) -> None:
        payload = tool_result.to_llm_payload()
        if self.session.tool_payload_filter is not None:
            payload = self.session.tool_payload_filter(payload)
        tool_msg = {
            "role": "tool",
            "tool_call_id": tool_call.get("id", ""),
            "name": name,
            "content": json.dumps(payload, ensure_ascii=False),
        }
        self.session._messages.append(tool_msg)
        if state.llm_view is not None and state.llm_view is not self.session._messages:
            state.llm_view.append(tool_msg)
        state.messages = self.session._messages

    def _patch_last_assistant_content(self, text: str, state: TurnState) -> None:
        if self.session._messages and self.session._messages[-1].get("role") == "assistant":
            self.session._messages[-1]["content"] = text
        if (
            state.llm_view is not None
            and state.llm_view is not self.session._messages
            and state.llm_view
            and state.llm_view[-1].get("role") == "assistant"
        ):
            state.llm_view[-1]["content"] = text


def _text_only_messages(
    messages: list[dict[str, Any]],
    *,
    prompt_prefix_length: int,
) -> list[dict[str, Any]]:
    """Return a topic-routing view containing only textual content blocks."""
    out: list[dict[str, Any]] = []
    if (
        isinstance(prompt_prefix_length, bool)
        or not isinstance(prompt_prefix_length, int)
        or prompt_prefix_length < 0
        or prompt_prefix_length > len(messages)
    ):
        raise ValueError("prompt_prefix_length is outside the message view")
    for message in messages[prompt_prefix_length:]:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, list):
            item["content"] = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        out.append(item)
    return out


__all__ = ["TurnOps", "TurnState"]
