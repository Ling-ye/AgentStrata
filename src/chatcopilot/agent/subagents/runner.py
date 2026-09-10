"""Budgeted subagent execution."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from chatcopilot.core.config import ChatConfig
from chatcopilot.contracts.development import (
    DevelopmentTaskScope,
    development_task_scope,
    parse_write_scope,
)
from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder
from chatcopilot.agent.lifecycle import defer_lifecycle_intent
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.contracts.agent import (
    AgentTask,
    EventSink,
    DeferredLifecycleIntent,
    SpanFinished,
    SpanStarted,
)
from chatcopilot.agent.session import AgentSession
from chatcopilot.agent.turn_support import safe_emit
from chatcopilot.core.observability_redaction import omit_private_reasoning_messages
from chatcopilot.contracts.cancellation import CancellationRequested
from chatcopilot.agent.subagents.cache import GLOBAL_SUBAGENT_CACHE, build_cache_key
from chatcopilot.agent.subagents.context_pack import ContextPackBuilder
from chatcopilot.agent.subagents.result import (
    SubagentResultHolder,
    build_result_payload,
    build_submit_result_tool,
    dump_payload,
    validate_output,
)
from chatcopilot.agent.subagents.spec import CachePolicySpec, ContextPolicySpec
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.agent.tools.executor import BackgroundSubmitter, PermissionFilter, ToolExecutor
from chatcopilot.agent.tools.file_delivery import FileSender
from chatcopilot.agent.tools.registry import ToolRegistry
from chatcopilot.agent.tools.workspace_context import WorkspaceService
from chatcopilot.agent.trace import current_trace, new_span_id, new_trace_id
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.contracts.prompt import BotPromptProfile

if TYPE_CHECKING:
    from chatcopilot.agent.rag.provider import Retriever

_TERMINAL_FAILURE_STOPS = {
    "llm_error",
    "tool_failure_cap",
    "timeout_cap",
    "tool_call_cap",
    "iteration_cap",
}


@dataclass(frozen=True)
class SubagentRuntimeConfig:
    model_env_prefix: str | None
    max_model_turns: int
    max_tool_calls: int
    timeout_seconds: int
    max_output_chars: int


@dataclass(frozen=True)
class SubagentRunResult:
    ok: bool
    summary: str
    outputs: tuple[str, ...] = ()
    error_code: str | None = None
    cache_status: str = "miss"
    lifecycle_intents: tuple[DeferredLifecycleIntent, ...] = ()


class SubagentRunner:
    def __init__(
        self,
        *,
        main_llm: LLMClient,
        main_config: ChatConfig,
        tools: Sequence[ToolDef],
        llm_profiles: Mapping[str, LLMClient] | None = None,
        background_submitter: BackgroundSubmitter | None = None,
        permission_filter: PermissionFilter | None = None,
        file_sender: FileSender | None = None,
        workspace_service: WorkspaceService | None = None,
        memory_snapshot: str | None = None,
        retriever: Retriever | None = None,
    ) -> None:
        self._main_llm = main_llm
        self._llm_profiles = MappingProxyType(dict(llm_profiles or {}))
        self._main_config = main_config
        self._tools = tuple(tools)
        self._background_submitter = background_submitter
        self._permission_filter = permission_filter
        self._file_sender = file_sender
        self._workspace_service = workspace_service
        self._memory_snapshot = memory_snapshot
        self._retriever = retriever

    def run(
        self,
        *,
        session_id: str,
        subagent_name: str,
        task: TaskPack,
        role_prompt: str,
        allow_tool: Callable[[ToolDef], bool],
        config: SubagentRuntimeConfig,
        version: str = "1",
        context_policy: ContextPolicySpec | None = None,
        cache_policy: CachePolicySpec | None = None,
        cleanup_tools: Sequence[str] = (),
        unavailable_message: str | None = None,
        output_schema: dict | None = None,
        caller_role: str = "user",
    ) -> SubagentRunResult:
        if not isinstance(task, TaskPack):
            raise TypeError("subagent task must be a TaskPack")
        parent = current_trace()
        trace_id = parent.trace_id if parent is not None else new_trace_id()
        parent_span = parent.span_id if parent is not None else None
        sink = parent.sink if parent is not None else (lambda _event: None)
        span_id = new_span_id()
        depth = parent.depth + 1 if parent is not None else 0
        safe_emit(sink, SpanStarted(name=f"subagent:{subagent_name}", kind="subagent",
            trace_id=trace_id, span_id=span_id, parent_span_id=parent_span, depth=depth,
            observed_at=time.time(), backend="native",
            data={"input": task.to_dict(), "configuration": asdict(config)}))
        details: dict = {}
        try:
            result = self._execute(
                session_id=session_id,
                subagent_name=subagent_name,
                task=task,
                role_prompt=role_prompt,
                allow_tool=allow_tool,
                config=config,
                version=version,
                context_policy=context_policy,
                cache_policy=cache_policy,
                cleanup_tools=cleanup_tools,
                unavailable_message=unavailable_message,
                output_schema=output_schema,
                trace_id=trace_id,
                subagent_span=span_id,
                subagent_depth=depth,
                sink=sink,
                details=details,
                caller_role=caller_role,
            )
        except BaseException as exc:
            safe_emit(
                sink,
                SpanFinished(
                    name=f"subagent:{subagent_name}",
                    kind="subagent",
                    ok=False,
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span,
                    depth=depth,
                    observed_at=time.time(),
                    backend="native",
                    summary=str(exc),
                    data={
                        "status": "cancelled"
                        if isinstance(exc, CancellationRequested)
                        else "failed",
                        "error": {"code": type(exc).__name__, "message": str(exc)},
                    },
                ),
            )
            raise
        try:
            output = json.loads(result.summary)
        except (ValueError, TypeError):
            output = result.summary
        safe_emit(sink, SpanFinished(name=f"subagent:{subagent_name}", kind="subagent", ok=result.ok,
            trace_id=trace_id, span_id=span_id, parent_span_id=parent_span, depth=depth,
            observed_at=time.time(), backend="native",
            summary=str(output.get("summary", "")) if isinstance(output, dict) else str(output),
            data={"result": output, "cache_status": result.cache_status, "error_code": result.error_code, **details}))
        return result

    def _execute(
        self,
        *,
        trace_id: str,
        subagent_span: str,
        subagent_depth: int,
        sink: EventSink,
        details: dict,
        session_id: str,
        subagent_name: str,
        task: TaskPack,
        role_prompt: str,
        allow_tool: Callable[[ToolDef], bool],
        config: SubagentRuntimeConfig,
        version: str = "1",
        context_policy: ContextPolicySpec | None = None,
        cache_policy: CachePolicySpec | None = None,
        cleanup_tools: Sequence[str] = (),
        unavailable_message: str | None = None,
        output_schema: dict | None = None,
        caller_role: str = "user",
    ) -> SubagentRunResult:
        if not isinstance(task, TaskPack):
            raise TypeError("subagent task must be a TaskPack")
        task_pack = task
        context_policy = context_policy or ContextPolicySpec()
        cache_policy = cache_policy or CachePolicySpec()
        try:
            delegated_allowed_paths = parse_write_scope(task_pack.write_scope)
        except ValueError as exc:
            payload = {
                "ok": False,
                "error_code": "invalid_write_scope",
                "summary": str(exc),
                "findings": [],
                "evidence": [],
                "changes": [],
                "commands_run": [],
                "outputs": [],
                "risks": [str(exc)],
                "next_steps": ["Retry with repository-relative write_scope paths."],
                "confidence": "high",
                "cache_summary": "not_used",
            }
            return SubagentRunResult(
                ok=False,
                error_code="invalid_write_scope",
                summary=dump_payload(payload),
            )

        from chatcopilot.contracts.tools import tool_access_allowed

        work_tools = [
            tool
            for tool in self._tools
            if allow_tool(tool)
            and _allowed_for_subagent(tool, subagent_name)
            and (
                self._permission_filter(tool) is None
                if self._permission_filter is not None
                else tool_access_allowed(caller_role, tool.access)
            )
        ]
        if not work_tools and unavailable_message:
            payload = {
                "ok": False,
                "summary": unavailable_message,
                "findings": [],
                "evidence": [],
                "changes": [],
                "commands_run": [],
                "outputs": [],
                "risks": [],
                "limits": {"unavailable": True},
                "next_steps": [],
                "confidence": "low",
                "cache_summary": "not_used",
                "error_code": f"{subagent_name}_unavailable",
            }
            return SubagentRunResult(
                ok=False,
                error_code=f"{subagent_name}_unavailable",
                summary=dump_payload(payload),
            )

        holder = SubagentResultHolder()
        submit_tool = build_submit_result_tool(holder)
        tool_registry = ToolRegistry(
            (
                ToolProvider(
                    id="subagent.session",
                    packs={"runtime.session": (submit_tool, *work_tools)},
                    module=__name__,
                ),
            )
        )
        tool_snapshot = tool_registry.snapshot(tool_packs=("runtime.session",))
        allowed_tools = list(tool_snapshot.tools)

        pfp = hashlib.sha256(role_prompt.encode("utf-8")).hexdigest()[:16]
        llm = self._resolve_llm(config.model_env_prefix)
        main_llm_config = getattr(self._main_config, "llm", None)
        model_name = (
            getattr(getattr(llm, "config", None), "model", "")
            or getattr(main_llm_config, "model", "")
            or "unknown"
        )

        cache_key = ""
        cache_status = "skip"
        cache_skip_reason = ""
        if not cache_policy.enabled:
            cache_skip_reason = "cache_disabled"
        elif delegated_allowed_paths:
            cache_skip_reason = "write_scope_present"
        else:
            cache_status = "miss"
            cache_key = build_cache_key(
                subagent_name=subagent_name,
                version=version,
                model=model_name,
                prompt_fingerprint=pfp,
                tools=work_tools,
                task=task_pack,
                policy=cache_policy,
            )
            scope = getattr(self._workspace_service, "execution_scope", None)
            binding = repr((session_id, caller_role, scope, self._memory_snapshot))
            cache_key = hashlib.sha256((cache_key + binding).encode()).hexdigest()
            cached = GLOBAL_SUBAGENT_CACHE.get(cache_key)
            if cached is not None:
                return SubagentRunResult(
                    ok=True, summary=cached.value, outputs=cached.outputs,
                    cache_status="hit",
                )

        if cache_skip_reason:
            details["cache_skip_reason"] = cache_skip_reason

        tools_schema = list(tool_snapshot.openai_schema)
        prompt_plan = PromptPlanBuilder().build(
            PromptBuildInput(
                profile=BotPromptProfile(
                    identity=role_prompt,
                    response_style="Return the final result only through submit_result.",
                ),
                backend="native",
                model=model_name,
                role=caller_role,
                channel_kind="private",
                session_policy="Use only the supplied TaskPack and allowed tools.",
                memory=self._memory_snapshot or "",
                tool_names=tuple(tool.name for tool in allowed_tools),
                is_subagent=True,
            )
        )
        model_turns = config.max_model_turns
        timeout = config.timeout_seconds
        session = AgentSession(
            session_id=f"{session_id}:subagent:{subagent_name}",
            llm=llm,
            executor=ToolExecutor(
                tools=list(allowed_tools),
                caller_role_hint=caller_role,
                background_submitter=self._background_submitter,
                permission_filter=self._permission_filter,
                file_sender=self._file_sender,
                workspace_service=self._workspace_service,
            ),
            tools_schema=tools_schema,
            prompt_plan=prompt_plan,
            context_manager=ContextManager(
                max_context_tokens=min(
                    self._main_config.runtime.max_context_tokens,
                    context_policy.max_context_tokens,
                ),
                sliding_window_turns=max(1, context_policy.sliding_window_turns),
                tool_result_summary_max_tokens=min(
                    300,
                    self._main_config.runtime.tool_result_summary_max_tokens,
                ),
                summarize_prior_tool_results=True,
            ),
            max_tool_iterations=model_turns,
            hard_iteration_cap=model_turns,
            max_tool_calls=config.max_tool_calls,
            timeout_seconds=None,
            hard_timeout_seconds=timeout,
            stall_window_seconds=30,
            stream_first_turn=False,
            trace_id=trace_id,
            trace_parent_span_id=subagent_span,
            trace_depth=subagent_depth + 1,
        )

        rag_snippets: list[str] = []
        if self._retriever is not None:
            try:
                from chatcopilot.agent.rag.provider import render_rag_snippet
                hits = self._retriever.search(task_pack.objective, top_k=3)
                snippet = render_rag_snippet(hits)
                if snippet:
                    rag_snippets = [snippet]
            except Exception:  # noqa: BLE001
                pass

        context_pack = ContextPackBuilder().build(
            task=task_pack,
            tools=work_tools,
            policy=context_policy,
            memory_summary=self._memory_snapshot,
            rag_snippets=rag_snippets,
        )
        cleanup_errors: list[str] = []
        task_scope = DevelopmentTaskScope(
            allowed_paths=delegated_allowed_paths,
            shell_profile="validation",
            task_label=subagent_name,
        )
        with development_task_scope(task_scope):
            try:
                result = session.run_task(
                    AgentTask(text=_frame_task(context_pack.render())),
                    on_event=sink if sink is not None else (lambda _event: None),
                )
            finally:
                for tool_name in cleanup_tools:
                    if any(tool.name == tool_name for tool in work_tools):
                        try:
                            cleanup_result = session.executor.execute(tool_name, {})
                            if not cleanup_result.ok:
                                cleanup_errors.append(
                                    f"{tool_name}: {cleanup_result.error or cleanup_result.summary}"
                                )
                        except Exception as exc:  # noqa: BLE001
                            cleanup_errors.append(f"{tool_name}: {type(exc).__name__}: {exc}")

        lifecycle_intents = tuple(
            replace(intent, source="subagent")
            for intent in result.lifecycle_intents
        )
        for intent in lifecycle_intents:
            defer_lifecycle_intent(intent)

        stop_error = None if result.stop_reason == "end_turn" else result.stop_reason
        mcp_error = _extract_mcp_error_code(session)
        submitted_ok = bool(holder.payload.get("ok", True)) if holder.payload is not None else True
        submitted_error = str((holder.payload or {}).get("error_code") or "").strip() or None
        if holder.payload is not None and submitted_ok:
            error_code = submitted_error or None
        else:
            error_code = stop_error or submitted_error or (mcp_error if not submitted_ok else None)
        payload = build_result_payload(
            holder=holder,
            final_text=result.final_text,
            ok=result.stop_reason not in _TERMINAL_FAILURE_STOPS,
            error_code=error_code,
            max_chars=config.max_output_chars,
        )
        if cleanup_errors:
            limits = payload.setdefault("limits", {})
            if isinstance(limits, dict):
                limits["cleanup_errors"] = cleanup_errors

        if holder.payload is None and error_code in _TERMINAL_FAILURE_STOPS:
            partial = _extract_partial_findings(session)
            if partial:
                payload["partial_findings"] = partial
                existing_summary = payload.get("summary") or ""
                payload["summary"] = (
                    "预算耗尽，未能完成结构化总结。已搜索到以下部分信息：\n"
                    + "\n".join(
                        f"- [{p.get('title') or p.get('source', '?')}] {p.get('snippet', '')}"
                        for p in partial[:5]
                    )
                    + (f"\n\n原始停止原因：{existing_summary}" if existing_summary else "")
                )

        produced = tuple(resource.path for resource in result.produced_resources)
        raw_outputs = payload.get("outputs")
        existing_outputs = raw_outputs if isinstance(raw_outputs, (list, tuple)) else ()
        outputs = tuple(dict.fromkeys([*existing_outputs, *produced]))
        payload["outputs"] = list(outputs)
        payload.setdefault("cache_summary", cache_status)

        output_warnings = validate_output(payload, output_schema)
        output_validation = "pass" if not output_warnings else "warn"
        details.update(output_validation=output_validation, stop_reason=result.stop_reason)
        details["transcript"] = omit_private_reasoning_messages(session.snapshot_messages()).messages
        if output_warnings:
            details["output_warnings"] = output_warnings

        summary = dump_payload(payload)
        if cache_key and bool(payload.get("ok")):
            GLOBAL_SUBAGENT_CACHE.set(
                cache_key,
                value=summary,
                outputs=outputs,
                ttl_seconds=cache_policy.ttl_seconds,
            )

        return SubagentRunResult(
            ok=bool(payload.get("ok")),
            error_code=error_code,
            summary=summary,
            outputs=outputs,
            cache_status=cache_status,
            lifecycle_intents=lifecycle_intents,
        )

    def _resolve_llm(self, model_env_prefix: str | None) -> LLMClient:
        if not model_env_prefix:
            return self._main_llm
        try:
            return self._llm_profiles[model_env_prefix]
        except KeyError as exc:
            raise ValueError(f"subagent LLM profile was not materialized: {model_env_prefix}") from exc


def _extract_partial_findings(session: AgentSession) -> list[dict]:
    """Extract search/extract results from a subagent's internal tool messages.

    When a subagent hits its budget cap before calling submit_result, the
    intermediate search results are locked inside its message history. This
    function scans those messages and returns structured partial findings so
    the main agent can make informed decisions (e.g., recognizing that the
    information does not exist rather than retrying blindly).
    """
    import json as _json

    findings: list[dict] = []
    for msg in session._messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not content or len(content) < 20:
            continue
        try:
            data = _json.loads(content)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        # Tavily search results
        results = data.get("results")
        if isinstance(results, list):
            for r in results[:3]:
                if isinstance(r, dict):
                    findings.append({
                        "source": str(r.get("url") or ""),
                        "title": str(r.get("title") or ""),
                        "snippet": str(r.get("content") or "")[:300],
                    })
            continue

        # Tavily extract / generic text content
        raw_content = data.get("raw_content") or data.get("text") or data.get("content")
        if isinstance(raw_content, str) and len(raw_content) > 50:
            title = str(data.get("title") or data.get("url") or "extracted")
            findings.append({
                "source": str(data.get("url") or ""),
                "title": title,
                "snippet": raw_content[:300],
            })

    return findings[:8]


_MCP_FAILURE_CODES = frozenset(
    {"mcp_quota_exceeded", "mcp_unavailable", "mcp_timeout", "mcp_busy"}
)


def _extract_mcp_error_code(session: AgentSession) -> str | None:
    """Find a stable MCP failure code inside nested ToolResult JSON payloads."""
    import json as _json

    def scan(value):
        if isinstance(value, dict):
            code = str(value.get("error_code") or "").strip()
            if code in _MCP_FAILURE_CODES:
                return code
            for child in value.values():
                found = scan(child)
                if found:
                    return found
            return None
        if isinstance(value, list):
            for child in value:
                found = scan(child)
                if found:
                    return found
            return None
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                return scan(_json.loads(value))
            except (TypeError, ValueError):
                return None
        return None

    for message in reversed(session._messages):
        if message.get("role") != "tool":
            continue
        found = scan(message.get("content"))
        if found:
            return found
    return None


def _frame_task(context_pack: str) -> str:
    return (
        "Delegated task context pack:\n"
        f"{context_pack.strip()}\n\n"
        "Complete the task within budget. You must call submit_result as the final "
        "structured handoff; do not finish with natural language only."
    )


def _allowed_for_subagent(tool: ToolDef, subagent_name: str) -> bool:
    if tool.category != "mcp":
        return True
    exposure = str(tool.metadata.get("mcp_exposure", "subagent"))
    if exposure == "disabled":
        return False
    server_id = str(tool.metadata.get("mcp_server_id", ""))
    if server_id and subagent_name == f"search_{server_id}":
        whitelist = tuple(tool.metadata.get("mcp_search_only_tools") or ())
        if whitelist:
            remote_name = str(tool.metadata.get("mcp_remote_name", ""))
            return remote_name in whitelist
        return True
    allowed = tuple(str(item) for item in tool.metadata.get("mcp_allowed_subagents", ()) or ())
    if not allowed:
        return exposure == "main"
    return subagent_name in allowed


__all__ = ["SubagentRunResult", "SubagentRunner", "SubagentRuntimeConfig"]
