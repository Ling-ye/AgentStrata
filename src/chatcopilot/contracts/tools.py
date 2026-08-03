"""Tool contracts shared by agent and external tools."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class DocAnchors:
    usage: str
    outputs: Dict[str, str] = field(default_factory=dict)
    fallback_output: Optional[str] = None


@dataclass(frozen=True)
class ToolContext:
    """Explicit runtime context passed to tool handlers.

    The current executor still supports legacy one-argument handlers while tool
    packages are migrated. New tools should accept ``(args, ctx)``.
    """

    workspace: Any = None
    workspace_root: Path | None = None
    file_sender: Any = None
    background_submitter: Any = None
    caller_role: str = "user"
    job: Any = None


HandlerResult = Tuple[str, List[str], Optional[str]]
Handler = Callable[..., HandlerResult]

EXECUTION_SYNC = "sync"
EXECUTION_GLOBAL_SERIAL_BACKGROUND = "global_serial_background"
EXECUTION_USER_SERIAL_BACKGROUND = "user_serial_background"


class ToolHandlerError(RuntimeError):
    """Structured handler failure preserved by the executor and job worker."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        stage: str = "",
        details: Dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.stage = stage
        self.details = dict(details or {})


@dataclass
class ToolDef:
    name: str
    summary: str
    properties: Dict[str, Dict[str, Any]]
    required: List[str]
    handler: Handler
    aliases: List[str] = field(default_factory=list)
    doc_anchors: Optional[DocAnchors] = None
    requires_role: Optional[str] = None
    weight: str = "light"
    execution_policy: str = EXECUTION_SYNC
    category: str = ""
    owner: str = ""
    module: str = ""
    deprecated: bool = False
    artifact_kinds: Tuple[str, ...] = ("file", "directory")
    metadata: Dict[str, Any] = field(default_factory=dict)


_TYPE_MAP: Dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def action_to_property(action: argparse.Action) -> Optional[Dict[str, Any]]:
    if isinstance(action, argparse._HelpAction):
        return None
    prop: Dict[str, Any] = {}
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        prop["type"] = "boolean"
    else:
        py_type = action.type if action.type is not None else str
        prop["type"] = _TYPE_MAP.get(py_type, "string")
    if action.choices:
        prop["enum"] = list(action.choices)
    if action.help:
        prop["description"] = action.help
    if action.default is not None and action.default != argparse.SUPPRESS:
        if isinstance(action.default, (str, int, float, bool)):
            prop["default"] = action.default
    return prop


def index_actions_by_dest(parser: argparse.ArgumentParser) -> Dict[str, argparse.Action]:
    out: Dict[str, argparse.Action] = {}
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.dest:
            out[action.dest] = action
    return out


def properties_from_argparse(
    parser: argparse.ArgumentParser,
    dests: List[str],
) -> Dict[str, Dict[str, Any]]:
    actions = index_actions_by_dest(parser)
    properties: Dict[str, Dict[str, Any]] = {}
    for dest in dests:
        action = actions.get(dest)
        if action is None:
            continue
        prop = action_to_property(action)
        if prop is None:
            continue
        properties[dest] = prop
    return properties


def _description_with_aliases(tool: ToolDef) -> str:
    description = tool.summary
    if tool.deprecated:
        description = "[Deprecated] " + description
    if tool.aliases:
        description += "\n中文/英文别名：" + " / ".join(tool.aliases)
    return description


def build_openai_schema(tool: ToolDef) -> Dict[str, Any]:
    sorted_props = dict(sorted(tool.properties.items()))
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": _description_with_aliases(tool),
            "parameters": {
                "type": "object",
                "properties": sorted_props,
                "required": [d for d in tool.required if d in tool.properties],
                "additionalProperties": False,
            },
        },
    }


def build_mcp_schema(tool: ToolDef) -> Dict[str, Any]:
    openai = build_openai_schema(tool)
    fn = openai["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "inputSchema": fn["parameters"],
    }


__all__ = [
    "DocAnchors",
    "EXECUTION_GLOBAL_SERIAL_BACKGROUND",
    "EXECUTION_SYNC",
    "EXECUTION_USER_SERIAL_BACKGROUND",
    "Handler",
    "HandlerResult",
    "ToolContext",
    "ToolDef",
    "ToolHandlerError",
    "action_to_property",
    "build_mcp_schema",
    "build_openai_schema",
    "index_actions_by_dest",
    "properties_from_argparse",
]
