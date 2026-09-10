"""Static controlled tool adapters; task files supply data, never executable code."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from chatcopilot.contracts.tool_packs import ToolProvider
from chatcopilot.contracts.tools import ToolDef, ToolResult, object_schema
from chatcopilot.contracts.tool_validation import validate_tool_contract
from chatcopilot.evals.business_dataset import tool_dependencies
from chatcopilot.evals.models import EvalCase

SUPPORTED_TOOLS = frozenset({"lookup_eval_fact"})


def readiness(case: EvalCase) -> dict[str, Any]:
    missing = sorted(set(tool_dependencies(case)) - SUPPORTED_TOOLS)
    return {
        "ready": not missing,
        "state": "not_configured" if missing else "ready",
        "missing_tools": missing,
        "environment": "固定只读测试数据 · 非真实平台",
        "reason": "未配置工具：" + ", ".join(missing) if missing else "",
    }


def fixture_rows(case: EvalCase) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for resource in case.metadata["business"]["resources"]:
        data = resource["data"]
        if set(data) != {"facts"} or not isinstance(data["facts"], dict):
            raise ValueError("lookup_eval_fact resource requires facts mapping")
        for key, row in data["facts"].items():
            if key in rows or not isinstance(row, dict) or set(row) - {"ok", "value", "error"}:
                raise ValueError("duplicate or invalid fixture fact")
            if (
                type(row.get("ok")) is not bool
                or not isinstance(row.get("value"), str)
                or not isinstance(row.get("error", ""), str)
            ):
                raise ValueError("fixture fact requires ok, text value and optional error")
            rows[key] = row
    return rows


def preflight(*, cases: tuple[EvalCase, ...] | list[EvalCase]) -> None:
    for case in cases:
        state = readiness(case)
        if not state["ready"]:
            raise ValueError(state["reason"])
        fixture_rows(case)


def build_provider(case: EvalCase, audit: list[dict[str, Any]]) -> ToolProvider:
    preflight(cases=[case])
    rows = fixture_rows(case)

    def lookup(arguments, context):
        key = str(arguments.get("key", ""))
        row = rows.get(key, {"ok": False, "value": "", "error": "fact_not_found"})
        result = ToolResult(
            ok=row["ok"],
            data={"value": row["value"]},
            error=row.get("error") or None,
            error_code="" if row["ok"] else "fixture_query_failed",
        )
        audit.append(
            {
                "name": "lookup_eval_fact",
                "arguments": deepcopy(arguments),
                "ok": result.ok,
                "result": deepcopy(result.data),
                "error": result.error,
            }
        )
        return result

    tool = ToolDef(
        name="lookup_eval_fact",
        summary="Return one deterministic evaluation fact by exact key.",
        input_schema=object_schema({"key": {"type": "string"}}, required=("key",)),
        output_schema=object_schema({"value": {"type": "string"}}, required=("value",)),
        handler=lookup,
        category="eval.deterministic",
        owner="evals",
        module=__name__,
    )
    violations = validate_tool_contract(tool)
    if violations:
        raise ValueError(f"invalid controlled tool contract: {violations}")
    tools = (tool,) if tool.name in tool_dependencies(case) else ()
    return ToolProvider(
        id="evals.business",
        module=__name__,
        packs={"runtime.session": tools},
        description="File-authored fixed read-only business fixtures",
    )
