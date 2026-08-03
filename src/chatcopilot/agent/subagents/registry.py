"""Subagent delegate and workflow tool registration facade.

Search subagents are auto-generated from MCP servers with ``risk: search``.
Each ``search_{server_id}`` subagent is hard-isolated to its own MCP source.
"""

from __future__ import annotations

import logging
from datetime import date
from dataclasses import replace
from typing import TYPE_CHECKING, Sequence

from chatcopilot.core.config import ChatConfig
from chatcopilot.core.llm_client import LLMClient
from chatcopilot.component_catalog.subagents import BUILTIN_SUBAGENTS
from chatcopilot.agent.subagents.definition_catalog import (
    BUILTIN_WORKFLOWS,
    apply_override as _apply_override_impl,
    custom_to_definition as _custom_to_definition_impl,
    iter_definitions as _iter_definitions_impl,
    iter_workflows as _iter_workflows_impl,
)
from chatcopilot.agent.subagents.delegate_tools import (
    availability_hint as _availability_hint_impl,
    delegate_payload as _delegate_payload_impl,
    has_write_selector as _has_write_selector_impl,
    make_delegate_tool as _make_delegate_tool_impl,
    summary_with_availability as _summary_with_availability_impl,
    with_web_fallback as _with_web_fallback_impl,
)
from chatcopilot.agent.subagents.runner import SubagentRuntimeConfig, SubagentRunner
from chatcopilot.agent.subagents.search_circuit import SearchCircuitBreaker
from chatcopilot.agent.subagents.search_factory import (
    build_search_prompt as _build_search_prompt,
    build_search_subagent as _build_search_subagent,
    collect_search_servers as _collect_search_servers,
)
from chatcopilot.agent.subagents.selector import build_predicate
from chatcopilot.agent.subagents.spec import SubagentDef, WorkflowDef
from chatcopilot.agent.subagents.task_pack import TaskPack
from chatcopilot.agent.subagents.workflow import WorkflowRunner
from chatcopilot.agent.subagents.workflow_tools import make_workflow_tool as _make_workflow_tool_impl
from chatcopilot.agent.tools.executor import BackgroundSubmitter, PermissionFilter
from chatcopilot.agent.tools.file_delivery import FileSender
from chatcopilot.agent.tools.workspace_context import WorkspaceService
from chatcopilot.contracts.runtime import McpServerConfig
from chatcopilot.contracts.subagents import CustomSubagentSpec, SubagentSpec
from chatcopilot.external_tools.shared.tool_spec import ToolDef

if TYPE_CHECKING:
    from chatcopilot.agent.rag.provider import Retriever

_LOG = logging.getLogger(__name__)


def build_subagent_tools(
    *,
    session_id: str,
    subagents: SubagentSpec,
    main_llm: LLMClient,
    main_config: ChatConfig,
    base_tools: Sequence[ToolDef],
    mcp_configs: Sequence[McpServerConfig] = (),
    background_submitter: BackgroundSubmitter | None = None,
    permission_filter: PermissionFilter | None = None,
    file_sender: FileSender | None = None,
    workspace_service: WorkspaceService | None = None,
    memory_snapshot: str | None = None,
    retriever: Retriever | None = None,
    search_circuit: "SearchCircuitBreaker | None" = None,
) -> tuple[ToolDef, ...]:
    has_search = bool(_collect_search_servers(base_tools, mcp_configs))
    if not subagents.include and not subagents.custom and not subagents.workflows and not has_search:
        return ()
    runner = SubagentRunner(
        main_llm=main_llm,
        main_config=main_config,
        tools=base_tools,
        background_submitter=background_submitter,
        permission_filter=permission_filter,
        file_sender=file_sender,
        workspace_service=workspace_service,
        memory_snapshot=memory_snapshot,
        retriever=retriever,
    )
    tools: list[ToolDef] = []
    seen_tool_names: set[str] = set()

    definitions: dict[str, SubagentDef] = {}
    configs: dict[str, SubagentRuntimeConfig] = {}
    predicates = {}
    for definition, budget in _iter_definitions(subagents):
        if definition.name in definitions:
            continue
        config = SubagentRuntimeConfig(
            model_env_prefix=budget.model_env_prefix,
            max_model_turns=budget.max_model_turns,
            max_tool_calls=budget.max_tool_calls,
            timeout_seconds=budget.timeout_seconds,
            max_output_chars=budget.max_output_chars,
        )
        definitions[definition.name] = definition
        configs[definition.name] = config
        predicates[definition.name] = build_predicate(definition.selector)
        if definition.tool_name in seen_tool_names:
            continue
        seen_tool_names.add(definition.tool_name)
        availability = _availability_hint(definition.name, base_tools, predicates[definition.name])
        tools.append(
            _make_delegate_tool(
                session_id,
                definition,
                runner,
                config,
                predicates[definition.name],
                availability_hint=availability,
            )
        )

    search_servers = _collect_search_servers(base_tools, mcp_configs)
    search_tools: dict[str, ToolDef] = {}
    for server_id, mcp_cfg in sorted(search_servers.items()):
        search_def, search_config = _build_search_subagent(
            mcp_cfg, subagents.search_budget,
        )
        if search_def.name in definitions:
            continue
        definitions[search_def.name] = search_def
        configs[search_def.name] = search_config
        predicates[search_def.name] = build_predicate(search_def.selector)
        if search_def.tool_name in seen_tool_names:
            continue
        seen_tool_names.add(search_def.tool_name)
        search_tools[server_id] = _make_delegate_tool(
            session_id,
            search_def,
            runner,
            search_config,
            predicates[search_def.name],
        )
        _LOG.info("auto-generated search subagent | name=%s server=%s", search_def.name, server_id)

    if "tavily" in search_tools and "searxng" in search_tools:
        search_tools["tavily"] = _with_web_fallback(
            primary=search_tools["tavily"],
            fallback=search_tools["searxng"],
            circuit=search_circuit or SearchCircuitBreaker(),
        )
    tools.extend(search_tools[server_id] for server_id in sorted(search_tools))

    workflow_runner = WorkflowRunner(
        runner=runner,
        definitions=definitions,
        configs=configs,
        predicates=predicates,
    )
    for workflow in _iter_workflows(subagents):
        if workflow.tool_name in seen_tool_names:
            continue
        seen_tool_names.add(workflow.tool_name)
        tools.append(_make_workflow_tool(session_id, workflow, workflow_runner))
    return tuple(tools)


def _iter_definitions(subagents: SubagentSpec):
    yield from _iter_definitions_impl(subagents, presets=BUILTIN_SUBAGENTS)


def _iter_workflows(subagents: SubagentSpec):
    yield from _iter_workflows_impl(subagents, workflows=BUILTIN_WORKFLOWS)


def _apply_override(definition: SubagentDef, custom: CustomSubagentSpec) -> SubagentDef:
    return _apply_override_impl(definition, custom)


def _custom_to_definition(custom: CustomSubagentSpec) -> SubagentDef:
    return _custom_to_definition_impl(custom)


def _make_delegate_tool(
    session_id: str,
    definition: SubagentDef,
    runner: SubagentRunner,
    config: SubagentRuntimeConfig,
    allow_tool,
    *,
    availability_hint: str = "",
) -> ToolDef:
    return _make_delegate_tool_impl(
        session_id,
        definition,
        runner,
        config,
        allow_tool,
        availability_hint=availability_hint,
        date_annotator=_with_current_date,
        module_name=__name__,
    )


def _with_current_date(task: TaskPack) -> TaskPack:
    today = date.today().isoformat()
    marker = (
        f"Authoritative current local date: {today}. Resolve 'latest', 'today', and "
        "other relative dates against this value."
    )
    cache_hint = f"{task.cache_key_hint}|date:{today}".strip("|")
    return replace(task, constraints=(*task.constraints, marker), cache_key_hint=cache_hint)


def _delegate_payload(summary: str) -> dict:
    return _delegate_payload_impl(summary)


def _with_web_fallback(
    *, primary: ToolDef, fallback: ToolDef, circuit: SearchCircuitBreaker
) -> ToolDef:
    return _with_web_fallback_impl(
        primary=primary,
        fallback=fallback,
        circuit=circuit,
        payload_parser=_delegate_payload,
    )


def _availability_hint(subagent_name: str, tools: Sequence[ToolDef], allow_tool) -> str:
    return _availability_hint_impl(subagent_name, tools, allow_tool)


def _summary_with_availability(summary: str, availability_hint: str) -> str:
    return _summary_with_availability_impl(summary, availability_hint)


def _make_workflow_tool(
    session_id: str,
    workflow: WorkflowDef,
    workflow_runner: WorkflowRunner,
) -> ToolDef:
    return _make_workflow_tool_impl(
        session_id,
        workflow,
        workflow_runner,
        module_name=__name__,
    )


def _has_write_selector(definition: SubagentDef) -> bool:
    return _has_write_selector_impl(definition)


__all__ = [
    "BUILTIN_WORKFLOWS",
    "SearchCircuitBreaker",
    "_build_search_prompt",
    "build_subagent_tools",
]
