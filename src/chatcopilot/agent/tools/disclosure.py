"""Session-bound projection of authorized tools for model-facing disclosure."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from chatcopilot.agent.context.token_estimator import estimate_tokens
from chatcopilot.contracts.tools import (
    TOOL_DISCLOSURE_BRIDGE_NAMES, ToolDef, ToolResult, build_openai_schema, object_schema,
)

BRIDGE_NAMES = TOOL_DISCLOSURE_BRIDGE_NAMES
_WORDS = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+")


def _query_terms(query: str) -> tuple[str, ...]:
    terms: list[str] = []
    for word in _WORDS.findall(query.casefold()):
        terms.append(word)
        if len(word) >= 3 and "\u3400" <= word[0] <= "\u9fff":
            terms.extend(word[index:index + 2] for index in range(len(word) - 1))
    return tuple(dict.fromkeys(terms))


def _schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters,
    }}


@dataclass(frozen=True)
class ToolDisclosureView:
    """Only tools surviving the session's Registry and host authorization projection."""

    tools: tuple[ToolDef, ...]
    pack_by_name: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "pack_by_name", MappingProxyType(dict(self.pack_by_name)))
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)) or BRIDGE_NAMES.intersection(names):
            raise ValueError("tool disclosure names must be unique and cannot shadow bridge tools")

    @property
    def deferred(self) -> dict[str, ToolDef]:
        return {tool.name: tool for tool in self.tools if tool.disclosure == "deferred"}

    def model_schemas(self) -> list[dict[str, Any]]:
        direct = [build_openai_schema(tool) for tool in self.tools if tool.disclosure == "direct"]
        if not self.deferred:
            return sorted(direct, key=lambda item: item["function"]["name"])
        manifest = self._manifest()
        bridge = [
            _schema("tool_search", "Find authorized host tools by task. Search English tool names or Chinese descriptions. " + manifest,
                object_schema({
                    "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                }, required=("queries",))),
            _schema("tool_describe", "Read the full input schema of authorized deferred host tools.",
                object_schema({"names": {"type": "array", "items": {"type": "string"},
                                         "minItems": 1, "maxItems": 5}}, required=("names",))),
            _schema("tool_call", "Call one authorized deferred host tool after checking its schema."
                " The host enforces the real tool's permissions and input schema.",
                object_schema({
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                }, required=("name", "arguments"))),
        ]
        return sorted([*direct, *bridge], key=lambda item: item["function"]["name"])

    def _manifest(self) -> str:
        grouped: dict[str, list[str]] = defaultdict(list)
        for tool in self.deferred.values():
            pack = self.pack_by_name.get(tool.name, tool.category or "other")
            grouped[pack].append(tool.name)
        detail = "Available: " + "; ".join(
            f"{pack} [{', '.join(sorted(items))}]" for pack, items in sorted(grouped.items())
        )
        if len(detail) <= 900 and estimate_tokens(detail) <= 1500:
            return detail
        packs = "Available packs: " + "; ".join(
            f"{pack} ({len(items)} tools)" for pack, items in sorted(grouped.items())
        )
        return packs if len(packs) <= 900 else f"Search {len(self.deferred)} authorized tools across {len(grouped)} packs."

    def search(self, args: Mapping[str, Any]) -> ToolResult:
        queries = args.get("queries")
        limit = args.get("limit", 5)
        if (not isinstance(queries, list) or not 1 <= len(queries) <= 3
                or any(not isinstance(q, str) or not q.strip() for q in queries)
                or type(limit) is not int or not 1 <= limit <= 10):
            return ToolResult(ok=False, error="invalid tool search arguments", error_code="tool_input_schema_invalid")
        results = []
        for query in queries:
            words = _query_terms(query)
            ranked = []
            for tool in self.deferred.values():
                pack = self.pack_by_name.get(tool.name, tool.category or "other")
                fields = (tool.name, *tool.aliases, pack, tool.summary)
                folded = tuple(str(field).casefold() for field in fields)
                score = sum(
                    (12 if word in folded[0] else 8 if any(word in alias for alias in folded[1:-2])
                     else 4 if word in folded[-2] else 1 if word in folded[-1] else 0)
                    for word in words
                )
                if score:
                    ranked.append((-score, tool.name, tool, pack))
            ranked.sort(key=lambda item: (item[0], item[1]))
            matches = [{"name": tool.name, "summary": tool.summary[:240], "pack": pack,
                        "required": list(tool.input_schema.get("required", []))}
                       for _score, _name, tool, pack in ranked[:limit]]
            results.append({"query": query, "matches": matches})
        packs = sorted({self.pack_by_name.get(tool.name, tool.category or "other")
                        for tool in self.deferred.values()})
        return ToolResult(ok=True, summary="工具目录检索完成。",
                          data={"results": results, "available_packs": packs})

    def describe(self, args: Mapping[str, Any]) -> ToolResult:
        names = args.get("names")
        if (not isinstance(names, list) or not 1 <= len(names) <= 5
                or any(not isinstance(name, str) or not name for name in names)):
            return ToolResult(ok=False, error="invalid tool description arguments",
                              error_code="tool_input_schema_invalid")
        deferred = self.deferred
        tools = {name: {"name": name, "summary": deferred[name].summary,
                        "input_schema": deferred[name].input_schema,
                        "pack": self.pack_by_name.get(name, deferred[name].category or "other")}
                 for name in dict.fromkeys(names) if name in deferred}
        return ToolResult(ok=True, summary="工具参数已载入。",
                          data={"tools": tools, "not_found": [name for name in names if name not in deferred]})

    def resolve_call(self, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | ToolResult:
        name, arguments = args.get("name"), args.get("arguments")
        if not isinstance(name, str) or name not in self.deferred:
            return ToolResult(ok=False, error="tool unavailable", error_code="tool_not_found")
        if not isinstance(arguments, dict):
            return ToolResult(ok=False, error="invalid tool arguments", error_code="tool_input_schema_invalid")
        return name, arguments
