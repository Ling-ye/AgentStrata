"""Build ToolDef wrappers for deterministic subagent workflows."""

from __future__ import annotations

from dataclasses import replace

from chatcopilot.agent.subagents.spec import WorkflowDef
from chatcopilot.agent.subagents.task_pack import parse_task_pack, task_pack_schema
from chatcopilot.agent.subagents.workflow import WorkflowRunner
from chatcopilot.external_tools.shared.tool_spec import EXECUTION_USER_SERIAL_BACKGROUND, ToolDef


def make_workflow_tool(
    session_id: str,
    workflow: WorkflowDef,
    workflow_runner: WorkflowRunner,
    *,
    module_name: str = __name__,
) -> ToolDef:
    def _handler(args: dict):
        task_args = dict(args)
        extension_inputs: list[str] = []
        if workflow.name == "coding":
            repository = str(task_args.pop("repository", "") or "").strip()
            change_id = str(task_args.pop("change_id", "") or "").strip()
            markers = [f"repository={repository}"]
            if change_id:
                markers.append(f"change_id={change_id}")
            extension_inputs.extend(markers)
        task = parse_task_pack(task_args)
        if extension_inputs:
            task = replace(
                task,
                inputs=(*task.inputs, *extension_inputs),
                write_scope=task.write_scope,
            )
        result = workflow_runner.run(
            session_id=session_id,
            workflow=workflow,
            task=task,
        )
        return result.summary, list(result.outputs), None

    properties = task_pack_schema()
    required = ["objective"]
    if workflow.name == "coding":
        properties.update(
            {
                "repository": {
                    "type": "string",
                    "description": "Registered writable repository id.",
                },
                "change_id": {
                    "type": "string",
                    "description": "Optional stable task change id.",
                    "default": "",
                },
            }
        )
        required.append("repository")
    return ToolDef(
        name=workflow.tool_name,
        summary=workflow.summary,
        properties=properties,
        required=required,
        handler=_handler,
        category="agent.subagent.workflow",
        owner="agent",
        module=module_name,
        artifact_kinds=("file", "directory"),
        requires_role="owner" if workflow.name == "coding" else None,
        weight="heavy" if workflow.name == "coding" else "light",
        execution_policy=(
            EXECUTION_USER_SERIAL_BACKGROUND if workflow.name == "coding" else "sync"
        ),
        metadata={"workflow": workflow.name, "workflow_steps": list(workflow.steps)},
    )


__all__ = ["make_workflow_tool"]
