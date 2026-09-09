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

from chatcopilot.core.observation_context import observe

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, cast

from chatcopilot.core.config import ChatConfig, LLMConfig
from chatcopilot.agent.capabilities import (
    RuntimeCapabilityContext,
    SessionCapabilityContext,
    materialize_runtime_providers,
    materialize_session_providers,
)
from chatcopilot.agent.context.prompt_plan import PromptBuildInput, PromptPlanBuilder
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.agent.mcp.client import McpToolProvider
from chatcopilot.agent.rag.provider import LocalTextRetriever, Retriever
from chatcopilot.agent.search.coordinator import SearchCoordinator
from chatcopilot.agent.search.tool import build_search_coordinator
from chatcopilot.agent.session import ToolPayloadFilter
from chatcopilot.agent.session_protocol import AgentSessionProtocol
from chatcopilot.agent.backends import BackendAgentSession, build_backend
from chatcopilot.agent.subagents.registry import SearchCircuitBreaker
from chatcopilot.agent.tools.executor import BackgroundSubmitter, PermissionFilter, ToolExecutor
from chatcopilot.agent.tools.file_delivery import FileSender
from chatcopilot.agent.tools.registry import ToolRegistry
from chatcopilot.agent.tools.workspace_context import WorkspaceService
from chatcopilot.contracts.runtime import McpServerConfig, RagSourceConfig
from chatcopilot.contracts.agent_backend import BackendOpenRequest, BackendSessionOptions
from chatcopilot.contracts.identity import SessionIdentity
from chatcopilot.contracts.subagents import SubagentSpec
from chatcopilot.contracts.skills import SkillIndexEntry
from chatcopilot.contracts.tool_packs import (
    TOOL_PACK_PROJECTION_PROFILES,
    ToolPackProjectionProfile,
    ToolProvider,
)
from chatcopilot.contracts.tools import (
    TOOL_AUDIENCE_MAIN,
    TOOL_AUDIENCE_SUBAGENT,
    ToolDef,
    build_openai_schema,
)
from chatcopilot.tool_packs.catalog import (
    BUILTIN_TOOL_PACKS,
    get_tool_pack_entry,
    session_tool_pack_entries,
)


_LOGGER = logging.getLogger(__name__)


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
    project_roots: tuple[Path, ...] = ()
    agent_backend: str = "native"
    tool_registry: ToolRegistry | None = field(default=None, repr=False)
    tool_packs: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    assembly_profile: ToolPackProjectionProfile = "interactive"
    session_capability_packs: tuple[str, ...] = ()
    search_llm: LLMClient | None = None
    subagent_llms: Mapping[str, LLMClient] = field(default_factory=dict, repr=False)
    search_provider_credentials: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.research_llm is None:
            self.research_llm = self.llm
        if self.search_llm is None:
            self.search_llm = self.research_llm
        self.subagent_llms = MappingProxyType(dict(self.subagent_llms))
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
        """Release instance resources, including aliased model clients, once."""
        if self._closed:
            return
        self._closed = True
        _close_resources((self.mcp_provider, self.retriever, self.llm,
                          self.research_llm, self.search_llm, *self.subagent_llms.values()))

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
            main_llm=self.search_llm or self.research_llm or self.llm,
            budget=self.subagents.research_budget,
            tools=self.tools,
            raw_mcp_tools=raw_mcp_search_tools,
            provider_specs=self.subagents.search_providers,
            turn_timeout_seconds=self.runtime_config.runtime.turn_timeout_seconds,
            max_wall_seconds=max_wall_seconds,
            circuit=self.search_circuit,
            semantic_rerank=False,
            provider_credentials=dict(self.search_provider_credentials),
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
            payload_filter: 由宿主为当前会话显式提供的 tool payload sanitizer。
            file_sender: 由 middleware 绑定当前平台 adapter 的文件回传回调，供
                ``send_files_to_user`` 工具使用；agent 不直接 import 平台。
            caller_identity: 当前入站消息的稳定身份；后端不得从角色提示反推身份。
            retriever_override: 会话级 RAG 投影。省略时使用 Bot 级 retriever；
                显式传 ``None`` 会关闭检索，供共享群等受限会话使用。
        """
        caller_role_hint = caller_role_hint or prompt_input.role
        if permission_filter is None:
            from chatcopilot.contracts.tools import tool_access_allowed

            def permission_filter(tool: ToolDef) -> str | None:
                return (
                    None
                    if tool_access_allowed(caller_role_hint, tool.access)
                    else "该工具仅限 Owner。"
                )

        memory_snippet = prompt_input.memory

        effective_retriever: Retriever | None
        if retriever_override is _USE_DEFAULT_RETRIEVER:
            effective_retriever = self.retriever
        else:
            effective_retriever = cast(Retriever | None, retriever_override)
        backend_id = (self.agent_backend or "native").strip().lower()
        if self.tool_registry is None:
            raise RuntimeError("AgentRuntime tool registry is not initialized")
        session_registry = ToolRegistry(self.tool_registry.providers.values())
        dynamic_pack_names: list[str] = []

        agent_providers = materialize_session_providers(
            SessionCapabilityContext(
                session_id=session_id,
                backend_id=backend_id,
                main_llm=self.llm,
                research_llm=self.research_llm or self.llm,
                runtime_config=self.runtime_config,
                search_llm=self.search_llm,
                subagent_llms=self.subagent_llms,
                search_provider_credentials=self.search_provider_credentials,
                subagents=self.subagents,
                base_tools=self.tools,
                subagent_tools=self.subagent_tools,
                mcp_configs=self.mcp_configs,
                memory_snapshot=memory_snippet,
                retriever=effective_retriever,
                search_circuit=self.search_circuit,
                background_submitter=background_submitter,
                permission_filter=permission_filter,
                file_sender=file_sender,
                workspace_service=workspace_service,
            ),
            tool_pack_names=self.session_capability_packs,
            profile=self.assembly_profile,
        )
        for provider in (*agent_providers, *session_providers):
            session_registry.register_runtime_provider(provider)
            dynamic_pack_names.extend(provider.pack_names)

        selected_packs = tuple(
            dict.fromkeys((*self.tool_packs, *dynamic_pack_names))
        )
        snapshot = session_registry.snapshot(
            tool_packs=selected_packs,
            exclude_tools=self.exclude_tools,
            require_all_selected=True,
            audience=TOOL_AUDIENCE_MAIN,
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
        visible_names = {tool.name for tool in visible_tools}
        observe(
            "session_registry",
            tools=[
                {
                    "name": tool.name,
                    "pack": snapshot.sources[tool.name].pack_id,
                    "provider": snapshot.sources[tool.name].provider_id,
                    "mcp_server_id": tool.metadata.get("mcp_server_id"),
                    "available": tool.name in visible_names,
                    "access": tool.access,
                    "parameters": tool.input_schema,
                }
                for tool in merged_tools
            ],
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
            llm=self.llm,
            tools_schema=merged_schema,
            retriever=effective_retriever,
        )
        options: BackendSessionOptions = {
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
            "execution_scope": getattr(workspace_service, "execution_scope", None),
        }
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
    search_llm_config: LLMConfig | None = None,
    subagent_llm_configs: Sequence[tuple[str, LLMConfig]] = (),
    search_provider_credentials: tuple[tuple[str, str], ...] = (),
    search_quota_max_ttl: float = 86400,
    tool_packs: Optional[Sequence[str]] = None,
    exclude_tools: Optional[Sequence[str]] = None,
    runtime_providers: Sequence[ToolProvider] = (),
    skill_index: Sequence[SkillIndexEntry] = (),
    rag_sources: Sequence[RagSourceConfig] = (),
    mcp_servers: Sequence[McpServerConfig] = (),
    subagents: Optional[SubagentSpec] = None,
    agent_backend: str = "native",
    assembly_profile: ToolPackProjectionProfile = "interactive",
    project_roots: tuple[Path, ...] = (),
) -> AgentRuntime:
    """装配一个 AgentRuntime。

    Args:
        chat_config: 上层加载的日常 LLM + 运行时配置。
        research_llm_config: 已解析的研究模型槽；为空时继承日常模型。
        tool_packs: BotSpec 声明的工具包白名单；为 None 时启用全部。
        exclude_tools: BotSpec 声明的工具黑名单。
        runtime_providers: 装配期按依赖构造的工具 provider。
        skill_index: BotSpec 解析出的 skill 索引，由当前 runtime 的 provider 闭包持有。
        rag_sources: BotSpec 声明的本地 RAG 知识源；为空时检索能力 no-op。
        mcp_servers: BotSpec 声明的 MCP server 绑定。
        subagents: BotSpec 声明的委托 Agent 配置。
        agent_backend: 主 Agent 实现选择；当前支持 native / langgraph / codex。
        assembly_profile: 宿主信任边界对应的能力投影；直接调用默认保持交互行为。
    """
    if assembly_profile not in TOOL_PACK_PROJECTION_PROFILES:
        raise ValueError(f"unknown Agent runtime assembly profile: {assembly_profile}")
    configured_subagents = subagents or SubagentSpec()
    budgets = [configured_subagents.agents.get(name, configured_subagents.defaults)
               for name in configured_subagents.include]
    budgets.extend(custom.budget for custom in configured_subagents.custom)
    if any(server.risk == "search" for server in mcp_servers):
        budgets.append(configured_subagents.search_budget)
    resolved_prefixes = {prefix for prefix, _ in subagent_llm_configs}
    if any(budget.model_env_prefix and budget.model_env_prefix not in resolved_prefixes
           for budget in budgets):
        raise ValueError("subagent model profile has not been resolved by the runtime host")
    if (configured_subagents.research_enabled
            and configured_subagents.research_budget.model_env_prefix
            and search_llm_config is None):
        raise ValueError("search model profile has not been resolved by the runtime host")
    for provider in runtime_providers:
        if not isinstance(provider, ToolProvider):
            continue
        for pack_id in provider.pack_names:
            entry = get_tool_pack_entry(pack_id)
            if entry is not None and assembly_profile not in entry.projection_profiles:
                raise ValueError(
                    "runtime provider pack is not allowed by the assembly profile: "
                    f"pack={pack_id}; profile={assembly_profile}"
                )

    declared_packs = tuple(
        dict.fromkeys(
            tuple(tool_packs) if tool_packs is not None else tuple(BUILTIN_TOOL_PACKS)
        )
    )
    requested_packs = tuple(
        name
        for name in declared_packs
        if (entry := get_tool_pack_entry(name)) is None
        or assembly_profile in entry.projection_profiles
    )
    session_capability_packs = tuple(
        entry.name
        for entry in session_tool_pack_entries(
            requested_packs,
            profile=assembly_profile,
        )
    )
    runtime_factory_packs = {
        entry.name
        for name in requested_packs
        if (entry := get_tool_pack_entry(name)) is not None
        and entry.runtime_scope == "runtime"
        and entry.provider_factory_module is not None
    }
    registry = ToolRegistry.from_catalog(
        tuple(name for name in requested_packs if name not in runtime_factory_packs)
    )
    for provider in materialize_runtime_providers(
        requested_packs,
        RuntimeCapabilityContext(skill_index=tuple(skill_index)),
    ):
        registry.register_runtime_provider(provider)
    if tool_packs is not None:
        selected_packs = list(requested_packs)
    else:
        registered_packs = frozenset(registry.pack_names)
        selected_packs = [
            name for name in requested_packs if name in registered_packs
        ]
    for provider in runtime_providers:
        registry.register_runtime_provider(provider)
        selected_packs.extend(provider.pack_names)
    selected_packs = list(dict.fromkeys(selected_packs))

    # Finish all repository-owned validation before starting any external MCP runner.
    registry.snapshot(
        tool_packs=selected_packs,
        exclude_tools=exclude_tools,
        audience=TOOL_AUDIENCE_MAIN,
    )
    registry.snapshot(
        tool_packs=selected_packs,
        exclude_tools=exclude_tools,
        audience=TOOL_AUDIENCE_SUBAGENT,
    )

    clients: list[tuple[LLMConfig, LLMClient]] = []

    def model_client(config: LLMConfig) -> LLMClient:
        for existing_config, client in clients:
            if config == existing_config:
                return client
        client = LLMClient(config)
        clients.append((replace(config), client))
        return client

    retriever = None
    mcp_provider = None
    try:
        llm = model_client(chat_config.llm)
        effective_research_config = research_llm_config or chat_config.llm
        research_llm = model_client(effective_research_config)
        search_llm = model_client(search_llm_config or effective_research_config)
        subagent_llms = {prefix: model_client(config) for prefix, config in subagent_llm_configs}
        search_circuit = SearchCircuitBreaker(quota_max_ttl=search_quota_max_ttl)
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
        if main_mcp_tools:
            selected_packs.append("mcp.dynamic")
        if mcp_runtime_provider is not None:
            registry.register_runtime_provider(mcp_runtime_provider)
        selected_packs = list(dict.fromkeys(selected_packs))
        snapshot = registry.snapshot(
            tool_packs=selected_packs,
            exclude_tools=exclude_tools,
            audience=TOOL_AUDIENCE_MAIN,
        )
        subagent_pack_names = list(selected_packs)
        if subagent_only_mcp_tools:
            subagent_pack_names.append("mcp.subagent")
        subagent_snapshot = registry.snapshot(
            tool_packs=tuple(dict.fromkeys(subagent_pack_names)),
            exclude_tools=exclude_tools,
            audience=TOOL_AUDIENCE_SUBAGENT,
        )
        return AgentRuntime(
            llm=llm,
            tools=snapshot.tools,
            tools_schema=snapshot.openai_schema,
            runtime_config=chat_config,
            research_llm=research_llm,
            search_llm=search_llm,
            subagent_llms=subagent_llms,
            search_provider_credentials=search_provider_credentials,
            search_circuit=search_circuit,
            retriever=retriever,
            skill_index=tuple(skill_index),
            subagents=configured_subagents,
            subagent_tools=subagent_snapshot.tools,
            mcp_provider=mcp_provider,
            mcp_configs=tuple(mcp_servers),
            agent_backend=agent_backend,
            tool_registry=registry,
            tool_packs=tuple(selected_packs),
            exclude_tools=tuple(exclude_tools or ()),
            assembly_profile=assembly_profile,
            project_roots=project_roots,
            session_capability_packs=session_capability_packs,
        )
    except BaseException:
        try:
            _close_resources((mcp_provider, retriever, *(client for _, client in clients)))
        except Exception:  # noqa: BLE001 - preserve the assembly failure
            _LOGGER.exception("Resource cleanup failed after AgentRuntime assembly error")
        raise


def _close_resources(resources: Sequence[object]) -> None:
    seen: set[int] = set()
    failure: Exception | None = None
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                if failure is None:
                    failure = exc
    if failure is not None:
        raise failure


def _hidden_by_search_entry(tool: ToolDef) -> bool:
    if tool.name in {
        "query_approved_sources",
        "browse_dynamic_page",
        "web_fetch_page",
    }:
        return True
    return tool.metadata.get("subagent_kind") == "search"


__all__ = ["AgentRuntime", "build_agent_runtime"]
