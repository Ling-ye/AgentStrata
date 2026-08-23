"""AgentRuntime：多协议共用的 agent 顶层入口。

中间件层（ACP / MCP / HTTP）拿到一个已装配好的 ``AgentRuntime`` 后，每次新建
会话调用 :meth:`AgentRuntime.new_session` 取得 ``AgentSession``。AgentRuntime
持有：

- LLMClient
- ToolExecutor + 全量 tools schema（融合 builtin + external_tools + mcp client）
- 可信 PromptBuildInput（由唯一 PromptPlanBuilder 构造不可变 plan）
- 可选的 SkillIndex（形成唯一 capability.skills layer）
- 可选的 tool_payload_filter（中间件按角色绑定，agent 不感知 Role）
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, cast

from chatcopilot.core.config import ChatConfig, LLMConfig
from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder
from chatcopilot.agent.context.topic import TopicLlm, TopicPolicy, TopicRelevanceClassifier
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.agent.mcp.client import McpToolProvider
from chatcopilot.agent.rag.provider import LocalTextRetriever, Retriever
from chatcopilot.agent.search.coordinator import SearchCoordinator
from chatcopilot.agent.search.tool import build_search_coordinator, build_search_provider
from chatcopilot.agent.session import AgentSession, ToolPayloadFilter
from chatcopilot.agent.session_protocol import AgentSessionProtocol
from chatcopilot.agent.backends import BackendAgentSession, build_backend
from chatcopilot.agent.skills.index import set_skill_index
from chatcopilot.agent.subagents.registry import (
    SearchCircuitBreaker,
    build_subagent_provider,
)
from chatcopilot.agent.tools.executor import BackgroundSubmitter, PermissionFilter, ToolExecutor
from chatcopilot.agent.tools.file_delivery import FileSender
from chatcopilot.agent.tools.registry import ToolRegistry
from chatcopilot.agent.tools.workspace_context import WorkspaceService
from chatcopilot.contracts.runtime import McpServerConfig, RagSourceConfig
from chatcopilot.contracts.agent_backend import BackendOpenRequest
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef, build_openai_schema


class _UseDefaultRetriever:
    pass


_USE_DEFAULT_RETRIEVER = _UseDefaultRetriever()


@dataclass
class AgentRuntime:
    """Agent 顶层入口：装配 LLM + tools schema + executor + skill/memory hooks。"""

    llm: LLMClient
    tools: tuple[ToolDef, ...]
    tools_schema: tuple[Dict[str, Any], ...]
    runtime_config: ChatConfig
    research_llm: LLMClient | None = None
    retriever: Optional[Retriever] = None
    skill_index: tuple[SkillIndexEntry, ...] = ()
    subagents: SubagentSpec = field(default_factory=SubagentSpec)
    subagent_tools: tuple[ToolDef, ...] = ()
    mcp_provider: Optional[McpToolProvider] = None
    mcp_configs: tuple[McpServerConfig, ...] = ()
    search_circuit: SearchCircuitBreaker = field(default_factory=SearchCircuitBreaker, repr=False)
    agent_backend: str = "native"
    tool_registry: ToolRegistry | None = field(default=None, repr=False)
    tool_packs: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    # 上层可在创建 runtime 之后再用 :meth:`bind_payload_filter_factory` 绑定按会话
    # 生成 sanitizer 的工厂；agent 不感知 Role 概念。
    _payload_filter_factory: Optional[Callable[[], Optional[ToolPayloadFilter]]] = field(
        default=None, repr=False
    )
    _background_submitter_factory: Optional[Callable[[str], BackgroundSubmitter]] = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.research_llm is None:
            self.research_llm = self.llm
        if self.tool_registry is None:
            registry = ToolRegistry()
            if self.tools:
                registry.register_runtime_provider(
                    ToolProvider(
                        id="runtime.supplied",
                        packs={"runtime.session": tuple(self.tools)},
                        module=__name__,
                    )
                )
                self.tool_packs = ("runtime.session",)
            self.tool_registry = registry

    def close(self) -> None:
        """Release long-lived resources (MCP runners, retriever, etc.)."""
        if self.mcp_provider is not None:
            self.mcp_provider.close()

    def bind_payload_filter_factory(
        self,
        factory: Callable[[], Optional[ToolPayloadFilter]],
    ) -> None:
        """绑定按会话生成 tool payload filter 的工厂。"""
        self._payload_filter_factory = factory

    def bind_background_submitter_factory(
        self,
        factory: Callable[[str], BackgroundSubmitter],
    ) -> None:
        """绑定按 session_id 生成后台任务提交器的工厂。"""
        self._background_submitter_factory = factory

    def build_unified_search_coordinator(
        self,
        *,
        max_wall_seconds: float | None = None,
    ) -> SearchCoordinator | None:
        """Expose the canonical search coordinator to trusted host workflows.

        This deliberately reuses the same provider registry, router, circuit
        breaker, and chat-model policy as ``search_information``.  It does not
        expose project/private-wiki tools to the persona research pipeline.
        """

        if not self.subagents.research_enabled:
            return None
        raw_mcp_search_tools = tuple(
            tool
            for tool in (self.subagent_tools or self.tools)
            if tool.category == "mcp"
            and str(tool.metadata.get("mcp_risk", "")) == "search"
        )
        return build_search_coordinator(
            main_llm=self.research_llm or self.llm,
            budget=self.subagents.research_budget,
            tools=self.tools,
            raw_mcp_tools=raw_mcp_search_tools,
            provider_specs=self.subagents.search_providers,
            turn_timeout_seconds=self.runtime_config.runtime.turn_timeout_seconds,
            max_wall_seconds=max_wall_seconds,
            circuit=self.search_circuit,
            semantic_rerank=False,
        )

    def new_session(
        self,
        *,
        session_id: str,
        prompt_input: PromptBuildInput,
        session_providers: Sequence[ToolProvider] = (),
        payload_filter: Optional[ToolPayloadFilter] = None,
        permission_filter: Optional[PermissionFilter] = None,
        background_submitter: Optional[BackgroundSubmitter] = None,
        file_sender: Optional[FileSender] = None,
        workspace_service: Optional[WorkspaceService] = None,
        caller_role_hint: Optional[str] = None,
        caller_identity: SessionIdentity | None = None,
        retriever_override: Retriever | None | _UseDefaultRetriever = (
            _USE_DEFAULT_RETRIEVER
        ),
    ) -> AgentSessionProtocol:
        """装配一个 AgentSession 实例。

        Args:
            session_id: 上层为本会话分配的 id（用于 transcript / debug 日志）。
            prompt_input: 已认证会话事实、Bot 表达层和动态上下文；本方法在工具
                投影完成后通过唯一 PromptPlanBuilder 构造不可变 PromptPlan。
            session_providers: 本次会话按依赖构造的工具 provider；所有工具仍通过
                同一个 Registry 快照进入 schema 与执行器。
            payload_filter: 角色化的 tool payload sanitizer；若为 None 则用绑定
                的 ``_payload_filter_factory`` 主动取一次。
            file_sender: 由 middleware 绑定当前平台 adapter 的文件回传回调，供
                ``send_files_to_user`` 工具使用；agent 不直接 import 平台。
            caller_identity: 当前入站消息的稳定身份；后端不得从角色提示反推身份。
            retriever_override: 会话级 RAG 投影。省略时使用 Bot 级 retriever；
                显式传 ``None`` 会关闭检索，供共享群等受限会话使用。
        """
        memory_snippet = prompt_input.memory

        if payload_filter is None and self._payload_filter_factory is not None:
            payload_filter = self._payload_filter_factory()
        if background_submitter is None and self._background_submitter_factory is not None:
            background_submitter = self._background_submitter_factory(session_id)
        effective_retriever: Retriever | None
        if retriever_override is _USE_DEFAULT_RETRIEVER:
            effective_retriever = self.retriever
        else:
            effective_retriever = cast(Retriever | None, retriever_override)
        backend_id = (self.agent_backend or "native").strip().lower()
        direct_codex = backend_id == "codex"
        codex_policy = self.subagents.codex
        allow_codex_delegate_tools = (
            direct_codex and codex_policy.allow_delegate_tools
        )
        allow_codex_unified_search = (
            direct_codex and codex_policy.allow_unified_search_tool
        )

        if self.tool_registry is None:
            raise RuntimeError("AgentRuntime tool registry is not initialized")
        session_registry = ToolRegistry(self.tool_registry.providers.values())
        dynamic_pack_names: list[str] = []

        delegate_provider: ToolProvider | None = None
        if (
            not direct_codex
            or allow_codex_delegate_tools
            or "adapter_forge" in self.subagents.include
        ):
            delegate_provider = build_subagent_provider(
                session_id=session_id,
                subagents=self.subagents,
                main_llm=self.llm,
                main_config=self.runtime_config,
                base_tools=self.subagent_tools or self.tools,
                mcp_configs=self.mcp_configs,
                background_submitter=background_submitter,
                permission_filter=permission_filter,
                file_sender=file_sender,
                workspace_service=workspace_service,
                memory_snapshot=memory_snippet,
                retriever=effective_retriever,
                search_circuit=self.search_circuit,
            )
            if (
                delegate_provider is not None
                and direct_codex
                and not allow_codex_delegate_tools
            ):
                delegate_tools = tuple(
                    tool
                    for tool in delegate_provider.packs["agent.delegation"]
                    if tool.metadata.get("subagent") == "adapter_forge"
                )
                delegate_provider = (
                    ToolProvider(
                        id=delegate_provider.id,
                        packs={"agent.delegation": delegate_tools},
                        module=delegate_provider.module,
                        description=delegate_provider.description,
                    )
                    if delegate_tools
                    else None
                )

        delegate_tools = (
            delegate_provider.packs["agent.delegation"]
            if delegate_provider is not None
            else ()
        )
        if delegate_provider is not None:
            session_registry.register_runtime_provider(delegate_provider)
            dynamic_pack_names.extend(delegate_provider.pack_names)

        accessible_delegate_tools = tuple(
            tool
            for tool in delegate_tools
            if permission_filter is None or permission_filter(tool) is None
        )
        search_provider: ToolProvider | None = None
        if self.subagents.research_enabled and (
            not direct_codex or allow_codex_unified_search
        ):
            accessible_base_tools = tuple(
                tool
                for tool in self.tools
                if permission_filter is None or permission_filter(tool) is None
            )
            raw_mcp_search_tools = tuple(
                tool
                for tool in (self.subagent_tools or self.tools)
                if tool.category == "mcp"
                and str(tool.metadata.get("mcp_risk", "")) == "search"
            )
            search_provider = build_search_provider(
                main_llm=self.research_llm or self.llm,
                budget=self.subagents.research_budget,
                tools=(
                    *accessible_base_tools,
                    *accessible_delegate_tools,
                ),
                raw_mcp_tools=raw_mcp_search_tools,
                provider_specs=self.subagents.search_providers,
                turn_timeout_seconds=(
                    self.runtime_config.runtime.turn_timeout_seconds
                ),
                circuit=self.search_circuit,
            )
        if search_provider is not None:
            session_registry.register_runtime_provider(search_provider)
            dynamic_pack_names.extend(search_provider.pack_names)
        for provider in session_providers:
            session_registry.register_runtime_provider(provider)
            dynamic_pack_names.extend(provider.pack_names)

        selected_packs = tuple(
            dict.fromkeys((*self.tool_packs, *dynamic_pack_names))
        )
        snapshot = session_registry.snapshot(
            tool_packs=selected_packs,
            exclude_tools=self.exclude_tools,
            require_all_selected=True,
        )
        merged_tools = list(snapshot.tools)
        search_tool = snapshot.index.get("search_information")
        visible_tools = [
            tool
            for tool in merged_tools
            if permission_filter is None or permission_filter(tool) is None
            if search_tool is None or not _hidden_by_search_entry(tool)
        ]
        merged_schema = sorted(
            (build_openai_schema(tool) for tool in visible_tools),
            key=lambda entry: str((entry.get("function") or {}).get("name") or ""),
        )
        effective_model = (
            str(self.runtime_config.routing.code_model or "").strip() or None
            if backend_id == "codex"
            else str(getattr(self.llm, "model", "") or "").strip() or None
        )
        prompt_plan = PromptPlanBuilder().build(
            replace(
                prompt_input,
                backend=backend_id,
                model=effective_model,
                memory=memory_snippet or "",
                tool_names=tuple(tool.name for tool in visible_tools),
            )
        )

        executor = ToolExecutor(
            tools=merged_tools,
            background_submitter=background_submitter,
            permission_filter=permission_filter,
            file_sender=file_sender,
            workspace_service=workspace_service,
            caller_role_hint=caller_role_hint,
        )

        rt = self.runtime_config.runtime
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
            TopicRelevanceClassifier(cast(TopicLlm, self.llm), topic_policy)
            if topic_policy.active
            else None
        )

        _defaults_rt = ChatConfig().runtime
        session_cls: type[AgentSession] | None = None
        if backend_id == "native":
            session_cls = AgentSession
        elif backend_id == "langgraph":
            from chatcopilot.agent.langgraph_session import LangGraphAgentSession

            session_cls = LangGraphAgentSession

        workspace_root = None
        backend_state_root = None
        isolate_backend_state = False
        if workspace_service is not None:
            requires_backend_state_isolation = getattr(
                workspace_service,
                "requires_backend_state_isolation",
                None,
            )
            isolation_required = (
                requires_backend_state_isolation() is True
                if callable(requires_backend_state_isolation)
                else False
            )

            def resolve_workspace_options() -> tuple[Path | None, Path | None]:
                workspace = workspace_service.resolve_workspace(create=True)
                object_root = getattr(workspace, "root", None)
                if object_root is None:
                    return None, None
                # The service-level resolver returns the aggregate instance root
                # used by Owner inventory tools. A member Codex sandbox must stay
                # inside the current chat/user workspace instead.
                resolved_workspace_root = Path(object_root).expanduser().resolve()
                resolve_backend_state_root = getattr(
                    workspace_service,
                    "resolve_backend_state_root",
                    None,
                )
                protected_root = (
                    resolve_backend_state_root()
                    if callable(resolve_backend_state_root)
                    else None
                )
                if not isinstance(protected_root, (str, Path)):
                    protected_root = None
                if protected_root is not None:
                    resolved_state_root = Path(protected_root).expanduser().resolve()
                elif isolation_required:
                    resolved_state_root = None
                else:
                    resolved_state_root = resolved_workspace_root / ".backend-sessions"
                return resolved_workspace_root, resolved_state_root

            if isolation_required:
                workspace_root, backend_state_root = resolve_workspace_options()
                if workspace_root is None or backend_state_root is None:
                    raise RuntimeError(
                        "isolated backend requires exact workspace and state roots"
                    )
                isolate_backend_state = True
            else:
                try:
                    workspace_root, backend_state_root = resolve_workspace_options()
                except Exception:  # noqa: BLE001 - legacy non-isolated fallback
                    workspace_root = None
                    backend_state_root = None
        backend = build_backend(
            backend_id,
            tool_names={tool.name for tool in visible_tools},
            runtime_config=self.runtime_config,
            tools=tuple(visible_tools),
            tool_executor=executor,
            tool_payload_filter=payload_filter,
            backend_policy=self.subagents.codex,
        )
        options: dict[str, Any] = {
            "workspace_root": workspace_root,
            "backend_state_root": backend_state_root,
            "isolate_backend_state": isolate_backend_state,
            # A shared-group actor cursor currently lives in the in-process
            # SessionState.  Reusing a persisted native thread after eviction
            # or process restart would therefore inject journal history from
            # sequence zero into a thread that already contains it.  Keep live
            # multi-turn resume, but start a fresh native thread whenever an
            # isolated actor backend is materialized again.
            "restore_persisted_native_session": not isolate_backend_state,
            "role_hint": caller_role_hint or "user",
        }
        if session_cls is not None:
            selected_session_cls = session_cls

            def session_factory() -> AgentSession:
                return selected_session_cls(
                    session_id=session_id,
                    llm=self.llm,
                    executor=executor,
                    tools_schema=merged_schema,
                    prompt_plan=prompt_plan,
                    tool_payload_filter=payload_filter,
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
                    retriever=effective_retriever,
                )

            options["session_factory"] = session_factory
        session_ref = backend.open_session(
            BackendOpenRequest(
                session_id=session_id,
                prompt_plan=prompt_plan,
                allowed_tool_names=frozenset(tool.name for tool in visible_tools),
                caller_identity=caller_identity,
                options=options,
            )
        )
        return BackendAgentSession(
            backend,
            session_ref,
            allowed_tool_names=frozenset(tool.name for tool in visible_tools),
        )


def build_agent_runtime(
    *,
    chat_config: ChatConfig,
    research_llm_config: LLMConfig | None = None,
    tool_packs: Optional[Sequence[str]] = None,
    exclude_tools: Optional[Sequence[str]] = None,
    runtime_providers: Sequence[ToolProvider] = (),
    skill_index: Sequence[SkillIndexEntry] = (),
    rag_sources: Sequence[RagSourceConfig] = (),
    mcp_servers: Sequence[McpServerConfig] = (),
    subagents: Optional[SubagentSpec] = None,
    agent_backend: str = "native",
) -> AgentRuntime:
    """装配一个 AgentRuntime。

    Args:
        chat_config: 上层加载的日常 LLM + 运行时配置。
        research_llm_config: 已解析的研究模型槽；为空时继承日常模型。
        tool_packs: BotSpec 声明的工具包白名单；为 None 时启用全部。
        exclude_tools: BotSpec 声明的工具黑名单。
        runtime_providers: 装配期按依赖构造的工具 provider。
        skill_index: BotSpec 解析出的 skill 索引，会同步写入 agent.skills 注册表。
        rag_sources: BotSpec 声明的本地 RAG 知识源；为空时检索能力 no-op。
        mcp_servers: BotSpec 声明的 MCP server 绑定。
        subagents: BotSpec 声明的委托 Agent 配置。
        agent_backend: 主 Agent 实现选择；当前支持 native / langgraph。
    """
    llm = LLMClient(chat_config.llm)
    effective_research_config = research_llm_config or chat_config.llm
    research_llm = (
        llm
        if effective_research_config == chat_config.llm
        else LLMClient(effective_research_config)
    )
    retriever = LocalTextRetriever(rag_sources) if rag_sources else None
    mcp_provider = McpToolProvider(tuple(mcp_servers)) if mcp_servers else None
    mcp_runtime_provider = (
        mcp_provider.load_provider() if mcp_provider is not None else None
    )
    main_mcp_tools = (
        mcp_runtime_provider.packs.get("mcp.dynamic", ())
        if mcp_runtime_provider is not None
        else ()
    )
    subagent_only_mcp_tools = (
        mcp_runtime_provider.packs.get("mcp.subagent", ())
        if mcp_runtime_provider is not None
        else ()
    )
    registry = ToolRegistry.from_catalog(tool_packs)
    selected_packs = list(
        tuple(tool_packs) if tool_packs is not None else registry.pack_names
    )
    if main_mcp_tools:
        selected_packs.append("mcp.dynamic")
    if mcp_runtime_provider is not None:
        registry.register_runtime_provider(mcp_runtime_provider)
    for provider in runtime_providers:
        registry.register_runtime_provider(provider)
        selected_packs.extend(provider.pack_names)
    selected_packs = list(dict.fromkeys(selected_packs))
    snapshot = registry.snapshot(
        tool_packs=selected_packs,
        exclude_tools=exclude_tools,
    )
    subagent_pack_names = [
        pack for pack in selected_packs if pack != "persona.control"
    ]
    if subagent_only_mcp_tools:
        subagent_pack_names.append("mcp.subagent")
    subagent_snapshot = registry.snapshot(
        tool_packs=tuple(dict.fromkeys(subagent_pack_names)),
        exclude_tools=exclude_tools,
    )
    set_skill_index(skill_index)
    return AgentRuntime(
        llm=llm,
        tools=snapshot.tools,
        tools_schema=snapshot.openai_schema,
        runtime_config=chat_config,
        research_llm=research_llm,
        retriever=retriever,
        skill_index=tuple(skill_index),
        subagents=subagents or SubagentSpec(),
        subagent_tools=subagent_snapshot.tools,
        mcp_provider=mcp_provider,
        mcp_configs=tuple(mcp_servers),
        agent_backend=agent_backend,
        tool_registry=registry,
        tool_packs=tuple(selected_packs),
        exclude_tools=tuple(exclude_tools or ()),
    )
def _hidden_by_search_entry(tool: ToolDef) -> bool:
    if tool.name in {
        "query_approved_sources",
        "browse_dynamic_page",
        "web_fetch_page",
    }:
        return True
    return tool.metadata.get("subagent_kind") == "search"


__all__ = ["AgentRuntime", "build_agent_runtime"]
