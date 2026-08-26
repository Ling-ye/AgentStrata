"""Resolve configured subagent presets, custom definitions, and workflows."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace

from chatcopilot.agent.subagents.spec import SubagentDef, WorkflowDef
from chatcopilot.component_catalog.subagents import (
    BUILTIN_SUBAGENT_WORKFLOWS,
    get_subagent_preset,
    get_workflow,
)
from chatcopilot.contracts.subagents import (
    CustomSubagentSpec,
    SubagentBudgetSpec,
    SubagentSpec,
)

BUILTIN_WORKFLOWS: Mapping[str, WorkflowDef] = BUILTIN_SUBAGENT_WORKFLOWS


def iter_definitions(
    subagents: SubagentSpec,
    *,
    presets: Mapping[str, SubagentDef] | None = None,
) -> Iterator[tuple[SubagentDef, SubagentBudgetSpec]]:
    for name in subagents.include:
        definition = (
            get_subagent_preset(name) if presets is None else presets.get(name)
        )
        if definition is None:
            continue
        override = subagents.overrides.get(name)
        if override is not None:
            definition = apply_override(definition, override)
        budget = subagents.agents.get(name, subagents.defaults)
        yield definition, budget
    for custom in subagents.custom:
        yield custom_to_definition(custom), custom.budget


def iter_workflows(
    subagents: SubagentSpec,
    *,
    workflows: Mapping[str, WorkflowDef] | None = None,
) -> Iterator[WorkflowDef]:
    for name in subagents.workflows:
        workflow = get_workflow(name) if workflows is None else workflows.get(name)
        if workflow is not None:
            yield replace(workflow, max_depth=subagents.max_workflow_depth)


def apply_override(definition: SubagentDef, custom: CustomSubagentSpec) -> SubagentDef:
    fields = set(custom.override_fields)
    return replace(
        definition,
        role_prompt=custom.role_prompt
        if "prompt" in fields and custom.role_prompt
        else definition.role_prompt,
        selector=custom.selector if "selector" in fields and not custom.selector.is_empty else definition.selector,
        context_policy=custom.context_policy if "context_policy" in fields else definition.context_policy,
        cache_policy=custom.cache_policy if "cache_policy" in fields else definition.cache_policy,
        workflow_tags=custom.workflow_tags if "workflow_tags" in fields else definition.workflow_tags,
        unavailable_message=custom.unavailable_message
        if "unavailable_message" in fields and custom.unavailable_message
        else definition.unavailable_message,
    )


def custom_to_definition(custom: CustomSubagentSpec) -> SubagentDef:
    return SubagentDef(
        name=custom.name,
        tool_name=custom.tool_name,
        summary=custom.summary,
        role_prompt=custom.role_prompt,
        kind=custom.kind,
        version=custom.version,
        selector=custom.selector,
        input_schema=custom.input_schema,
        output_schema=custom.output_schema,
        context_policy=custom.context_policy,
        cache_policy=custom.cache_policy,
        workflow_tags=custom.workflow_tags,
        unavailable_message=custom.unavailable_message,
    )


__all__ = [
    "BUILTIN_WORKFLOWS",
    "apply_override",
    "custom_to_definition",
    "iter_definitions",
    "iter_workflows",
]
