"""AgentRuntime：多协议共用的 agent 顶层入口。

中间件层（ACP / MCP / HTTP）拿到一个已装配好的 ``AgentRuntime`` 后，每次新建
会话调用 :meth:`AgentRuntime.new_session` 取得 ``AgentSession``。AgentRuntime
持有：

- LLMClient
- ToolExecutor + 全量 tools schema（融合 builtin + external_tools + mcp client）
- 可选的 MemoryProvider（让 AgentSession 在 system prompt 末尾注入记忆摘要）
- 可选的 SkillIndex（让 prompt builder 列出可按需读取的 skill）
- 可选的 tool_payload_filter（中间件按角色绑定，agent 不感知 Role）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from chatcopilot.core.config import ChatConfig
from chatcopilot.agent.context.manager import ContextManager
from chatcopilot.agent.context.prompt_builder import build_system_prompt
from chatcopilot.agent.quality_gate import build_quality_gate
from chatcopilot.agent.context.topic import TopicPolicy, TopicRelevanceClassifier
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.agent.memory.provider import MemoryProvider
from chatcopilot.agent.mcp.client import McpToolProvider
from chatcopilot.agent.rag.provider import LocalTextRetriever, Retriever
from chatcopilot.agent.search.tool import build_search_tool
from chatcopilot.agent.session import AgentSession, ToolPayloadFilter
from chatcopilot.agent.session_protocol import AgentSessionProtocol
from chatcopilot.agent.backends import BackendAgentSession, build_backend
from chatcopilot.agent.skills.index import set_skill_index
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker, build_subagent_tools
from chatcopilot.agent.tools.executor import BackgroundSubmitter, PermissionFilter, ToolExecutor
from chatcopilot.agent.tools.file_delivery import FileSender
from chatcopilot.agent.tools.registry import discover_tools
from chatcopilot.agent.tools.workspace_context import WorkspaceService
from chatcopilot.contracts.runtime import McpServerConfig, RagSourceConfig
from chatcopilot.contracts.agent_backend import BackendOpenRequest
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.external_tools.shared.tool_spec import ToolDef, build_openai_schema


@dataclass
class AgentRuntime:
    """Agent 顶层入口：装配 LLM + tools schema + executor + skill/memory hooks。"""

    llm: LLMClient
    tools: tuple[ToolDef, ...]
    tools_schema: tuple[Dict[str, Any], ...]
    runtime_config: ChatConfig
    memory_factory: Optional[Callable[[], MemoryProvider]] = None
    retriever: Optional[Retriever] = None
    skill_index: tuple[SkillIndexEntry, ...] = ()
    subagents: SubagentSpec = field(default_factory=SubagentSpec)
    subagent_tools: tuple[ToolDef, ...] = ()
    mcp_provider: Optional[McpToolProvider] = None
    mcp_configs: tuple[McpServerConfig, ...] = ()
    search_circuit: SearchCircuitBreaker = field(default_factory=SearchCircuitBreaker, repr=False)
    agent_backend: str = "native"
    # 上层可在创建 runtime 之后再用 :meth:`bind_payload_filter_factory` 绑定按会话
    # 生成 sanitizer 的工厂；agent 不感知 Role 概念。
    _payload_filter_factory: Optional[Callable[[], Optional[ToolPayloadFilter]]] = field(
        default=None, repr=False
    )
    _background_submitter_factory: Optional[Callable[[str], BackgroundSubmitter]] = field(
        default=None, repr=False
    )

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

    def new_session(
        self,
        *,
        session_id: str,
        system_baseline: str,
        session_dynamic_tail: Optional[str] = None,
        memory_snippet_override: Optional[str] = None,
        extra_tools: Sequence[ToolDef] = (),
        payload_filter: Optional[ToolPayloadFilter] = None,
        permission_filter: Optional[PermissionFilter] = None,
        background_submitter: Optional[BackgroundSubmitter] = None,
        file_sender: Optional[FileSender] = None,
        workspace_service: Optional[WorkspaceService] = None,
        caller_role_hint: Optional[str] = None,
        caller_identity: SessionIdentity | None = None,
        retriever_override: Optional[Retriever] = None,
    ) -> AgentSessionProtocol:
        """装配一个 AgentSession 实例。

        Args:
            session_id: 上层为本会话分配的 id（用于 transcript / debug 日志）。
            system_baseline: 上层提供的角色无关基线（机器人人格 + 安全规则 +
                capability 片段）；agent 内部会自动把 memory 摘要 + skill 索引
                拼到末尾。
            session_dynamic_tail: per-session 动态内容（如 persona overlay），
                放在 skill 索引之后、memory 之前，不破坏前面稳定前缀的 cache。
            memory_snippet_override: 上层显式提供的记忆摘要；为 None 时用
                ``memory_factory`` 主动取一次。
            extra_tools: 本次会话专属工具（如 ACP 的模式切换工具），与全局 tools
                合并后构造本会话的 ``ToolExecutor`` 与 schema。
            payload_filter: 角色化的 tool payload sanitizer；若为 None 则用绑定
                的 ``_payload_filter_factory`` 主动取一次。
            file_sender: 由 middleware 绑定当前平台 adapter 的文件回传回调，供
                ``send_files_to_user`` 工具使用；agent 不直接 import 平台。
            caller_identity: 当前入站消息的稳定身份；后端不得从角色提示反推身份。
        """
        memory_snippet = memory_snippet_override
        if memory_snippet is None and self.memory_factory is not None:
            try:
                memory_snippet = self.memory_factory().snapshot()
            except Exception:  # noqa: BLE001
                memory_snippet = ""

        if payload_filter is None and self._payload_filter_factory is not None:
            payload_filter = self._payload_filter_factory()
        if background_submitter is None and self._background_submitter_factory is not None:
            background_submitter = self._background_submitter_factory(session_id)
        effective_retriever = retriever_override or self.retriever
        backend_id = (self.agent_backend or "native").strip().lower()
        direct_codex = backend_id == "codex"

        delegate_tools = ()
        if not direct_codex or "adapter_forge" in self.subagents.include:
            delegate_tools = build_subagent_tools(
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
            if direct_codex:
                delegate_tools = tuple(
                    tool
                    for tool in delegate_tools
                    if tool.metadata.get("subagent") == "adapter_forge"
                )

        accessible_delegate_tools = tuple(
            tool
            for tool in delegate_tools
            if permission_filter is None or permission_filter(tool) is None
        )
        search_tool = None
        if self.subagents.research_enabled and not direct_codex:
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
            search_tool = build_search_tool(
                main_llm=self.llm,
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
        has_search_tools = search_tool is not None or any(
            t.metadata.get("subagent_kind") == "search" for t in accessible_delegate_tools
        )
        # For prompt text stability (prefix cache), derive routing tool names from
        # the declared MCP config rather than runtime-available tools. This way,
        # temporary MCP unavailability doesn't change the system prompt text.
        # Permission filtering is NOT applied here because the routing policy is
        # informational; denied tools simply won't appear in the schema.
        if self.subagents.research_enabled and not direct_codex:
            routing_tool_names: tuple[str, ...] = ("search_information",)
        else:
            declared_search_names = tuple(
                f"search_{cfg.id}"
                for cfg in sorted(self.mcp_configs, key=lambda c: c.id)
                if cfg.risk == "search"
            )
            routing_tool_names = declared_search_names or tuple(
                tool.name
                for tool in accessible_delegate_tools
                if tool.metadata.get("subagent_kind") == "search"
                or tool.name == "query_approved_sources"
            )

        def render_system_prompt(baseline: str) -> str:
            return build_system_prompt(
                baseline=baseline,
                skill_index=self.skill_index,
                memory_snippet=memory_snippet,
                has_search_tools=has_search_tools,
                search_tool_names=routing_tool_names,
                session_dynamic_tail=session_dynamic_tail,
            )

        system_prompt = render_system_prompt(system_baseline)

        merged_tools = [
            *self.tools,
            *delegate_tools,
            *([search_tool] if search_tool is not None else []),
            *extra_tools,
        ]
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
            TopicRelevanceClassifier(self.llm, topic_policy) if topic_policy.active else None
        )

        gate_level = getattr(rt, "quality_gate_level", 0)
        quality_gate = build_quality_gate(level=gate_level, llm=self.llm)

        _defaults_rt = ChatConfig().runtime
        session_cls = None
        if backend_id == "native":
            session_cls = AgentSession
        elif backend_id == "langgraph":
            from chatcopilot.agent.langgraph_session import LangGraphAgentSession

            session_cls = LangGraphAgentSession

        session_kwargs = dict(
            session_id=session_id,
            llm=self.llm,
            executor=executor,
            tools_schema=merged_schema,
            system_baseline=system_prompt,
            system_prompt_renderer=render_system_prompt,
            tool_payload_filter=payload_filter,
            context_manager=ctx_mgr,
            topic_classifier=topic_classifier,
            max_tool_iterations=max(
                1, getattr(rt, "max_tool_iterations", _defaults_rt.max_tool_iterations)
            ),
            hard_iteration_cap=max(
                1, getattr(rt, "hard_iteration_cap", _defaults_rt.hard_iteration_cap)
            ),
            max_tool_calls=getattr(rt, "max_tool_calls", _defaults_rt.max_tool_calls),
            timeout_seconds=getattr(rt, "turn_timeout_seconds", _defaults_rt.turn_timeout_seconds),
            hard_timeout_seconds=getattr(rt, "hard_timeout_seconds", _defaults_rt.hard_timeout_seconds),
            stall_window_seconds=max(
                10, getattr(rt, "stall_window_seconds", _defaults_rt.stall_window_seconds)
            ),
            max_consecutive_tool_failures=max(1, rt.max_tool_retries),
            retriever=effective_retriever,
            quality_gate=quality_gate,
        )
        workspace_root = None
        backend_state_root = None
        if workspace_service is not None:
            try:
                workspace = workspace_service.resolve_workspace(create=True)
                object_root = getattr(workspace, "root", None)
                if object_root is not None:
                    # The service-level resolver returns the aggregate instance root
                    # used by Owner inventory tools. A member Codex sandbox must stay
                    # inside the current chat/user workspace instead.
                    workspace_root = Path(object_root).expanduser().resolve()
                    backend_state_root = workspace_root / ".backend-sessions"
            except Exception:  # noqa: BLE001
                workspace_root = None
                backend_state_root = None
        backend = build_backend(
            backend_id,
            tool_names={tool.name for tool in visible_tools},
            runtime_config=self.runtime_config,
            tools=tuple(visible_tools),
            tool_executor=executor,
            backend_policy=self.subagents.codex,
        )
        options: dict[str, Any] = {
            "workspace_root": workspace_root,
            "backend_state_root": backend_state_root,
            "role_hint": caller_role_hint or "user",
        }
        if session_cls is not None:
            options["session_factory"] = lambda: session_cls(**session_kwargs)
        session_ref = backend.open_session(
            BackendOpenRequest(
                session_id=session_id,
                system_baseline=system_prompt,
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
    tool_packs: Optional[Sequence[str]] = None,
    exclude_tools: Optional[Sequence[str]] = None,
    extra_tools: Sequence[ToolDef] = (),
    skill_index: Sequence[SkillIndexEntry] = (),
    memory_factory: Optional[Callable[[], MemoryProvider]] = None,
    rag_sources: Sequence[RagSourceConfig] = (),
    mcp_servers: Sequence[McpServerConfig] = (),
    subagents: Optional[SubagentSpec] = None,
    agent_backend: str = "native",
) -> AgentRuntime:
    """装配一个 AgentRuntime。

    Args:
        chat_config: 上层加载的 LLM + 运行时配置。
        tool_packs: BotSpec 声明的工具包白名单；为 None 时启用全部。
        exclude_tools: BotSpec 声明的工具黑名单。
        extra_tools: 上层注入的会话本地工具（不进入全局注册中心）。
        skill_index: BotSpec 解析出的 skill 索引，会同步写入 agent.skills 注册表。
        memory_factory: 长期记忆 provider 工厂。每次 new_session 调用一次取 snapshot。
        rag_sources: BotSpec 声明的本地 RAG 知识源；为空时检索能力 no-op。
        mcp_servers: BotSpec 声明的 MCP server 绑定。
        subagents: BotSpec 声明的委托 Agent 配置。
        agent_backend: 主 Agent 实现选择；当前支持 native / langgraph。
    """
    llm = LLMClient(chat_config.llm)
    retriever = LocalTextRetriever(rag_sources) if rag_sources else None
    mcp_provider = McpToolProvider(tuple(mcp_servers)) if mcp_servers else None
    mcp_tools = mcp_provider.load_tools() if mcp_provider is not None else ()
    main_mcp_tools = tuple(tool for tool in mcp_tools if _mcp_visible_to_main(tool))
    subagent_only_mcp_tools = tuple(tool for tool in mcp_tools if tool not in main_mcp_tools)
    discovered = discover_tools(
        tool_packs=tool_packs,
        exclude_tools=exclude_tools,
        mcp_tools=main_mcp_tools,
    )
    merged = [*discovered, *extra_tools]
    subagent_base_tools = tuple([*discovered, *subagent_only_mcp_tools])
    schema = tuple(
        sorted(
            (build_openai_schema(tool) for tool in merged),
            key=lambda entry: str((entry.get("function") or {}).get("name") or ""),
        )
    )
    set_skill_index(skill_index)
    return AgentRuntime(
        llm=llm,
        tools=tuple(merged),
        tools_schema=schema,
        runtime_config=chat_config,
        memory_factory=memory_factory,
        retriever=retriever,
        skill_index=tuple(skill_index),
        subagents=subagents or SubagentSpec(),
        subagent_tools=subagent_base_tools,
        mcp_provider=mcp_provider,
        mcp_configs=tuple(mcp_servers),
        agent_backend=agent_backend,
    )


def _mcp_visible_to_main(tool: ToolDef) -> bool:
    if tool.category != "mcp":
        return True
    return str(tool.metadata.get("mcp_exposure", "subagent")) == "main"


def _hidden_by_search_entry(tool: ToolDef) -> bool:
    if tool.name in {
        "query_approved_sources",
        "browse_dynamic_page",
        "web_fetch_page",
    }:
        return True
    return tool.metadata.get("subagent_kind") == "search"


__all__ = ["AgentRuntime", "build_agent_runtime"]
