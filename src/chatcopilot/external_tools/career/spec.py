"""Tool definitions for the career intelligence capability."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.external_tools.career.service import CareerIntelService
from chatcopilot.external_tools.shared.spec_helpers import current_workspace
from chatcopilot.external_tools.shared.tool_spec import (
    ToolContext,
    ToolDef,
    ToolResult,
    object_schema,
)


def _service() -> CareerIntelService:
    return CareerIntelService(current_workspace(create=True).root)


_MAX_TOOL_RESULT_CHARS = 12000


def _result(payload: Any) -> ToolResult:
    bounded = _bounded_payload(payload)
    data = bounded if isinstance(bounded, dict) else {"result": bounded}
    ok = data.get("ok") is not False
    summary = str(data.get("summary") or data.get("message") or "职业情报操作完成。")
    return ToolResult(
        ok=ok,
        summary=summary if ok else "",
        data=data,
        error=None if ok else summary,
        error_code=str(data.get("error_code") or "career_operation_failed") if not ok else "",
        stage="execution" if not ok else "",
    )


def _bounded_payload(payload: Any) -> Any:
    value = deepcopy(payload)
    _clip_strings(value)
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    list_keys = (
        "new_jobs", "changed_jobs", "suspected_closed_jobs", "jobs", "evidence",
        "frequent_interview_questions", "salary_samples",
    )
    while len(text) > _MAX_TOOL_RESULT_CHARS:
        candidates = [
            (key, value.get(key)) for key in list_keys
            if isinstance(value, dict) and isinstance(value.get(key), list) and len(value[key]) > 1
        ]
        if not candidates:
            break
        key, items = max(candidates, key=lambda item: len(item[1]))
        value.setdefault("truncated", {})[key] = len(items)
        value[key] = items[: max(1, len(items) // 2)]
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return {
            "truncated": True,
            "message": "结果已保存到 career intelligence 数据库；请用更小 limit 或更窄过滤条件查询。",
            "result_chars_before_truncation": len(text),
        }
    return value


def _clip_strings(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str) and len(item) > 1000:
                value[key] = item[:980] + "...[truncated]"
            else:
                _clip_strings(item)
    elif isinstance(value, list):
        for item in value:
            _clip_strings(item)


def _list(args: dict[str, Any], key: str) -> list[str] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"{key} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def _watchlist_update(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    payload = _service().store.update_watchlist(
        companies=_list(args, "companies"),
        keywords=_list(args, "keywords"),
        locations=_list(args, "locations"),
        replace=bool(args.get("replace", False)),
    )
    return _result({"watchlist": payload})


def _watchlist_show(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    del args
    return _result({"watchlist": _service().store.get_watchlist()})


def _search_jobs(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    payload = _service().search_jobs(
        companies=_list(args, "companies"),
        keywords=_list(args, "keywords"),
        locations=_list(args, "locations"),
        posted_within_days=int(args.get("posted_within_days") or 30),
        limit_per_company=int(args.get("limit_per_company") or 20),
    )
    return _result(payload)


def _ingest(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    records = args.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records 必须是非空证据数组")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("records 每一项必须是对象")
    return _result(_service().store.ingest_evidence(records))


def _ingest_jobs(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    records = args.get("records")
    scan_scope = args.get("scan_scope")
    if not isinstance(records, list) or not records or not all(isinstance(item, dict) for item in records):
        raise ValueError("records 必须是非空岗位对象数组")
    if not isinstance(scan_scope, dict):
        raise ValueError("scan_scope 必须是对象")
    return _result(_service().ingest_jobs(records=records, scan_scope=scan_scope))


def _query(args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
    kind = str(args.get("kind") or "all")
    if kind not in {"all", "jobs", "evidence"}:
        raise ValueError("kind 仅支持 all/jobs/evidence")
    limit = int(args.get("limit") or 50)
    if limit < 1 or limit > 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    return _result(
        _service().store.query(
            companies=_list(args, "companies"),
            kind=kind,
            limit=limit,
            since_days=_int_arg(args, "since_days", 365),
            role_family=str(args.get("role_family") or "").strip(),
            detail=bool(args.get("detail", False)),
        )
    )


def _int_arg(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key)
    return default if value is None else int(value)


_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
_PAYLOAD_SCHEMA = {"type": "object", "additionalProperties": True}

TOOLS = [
    ToolDef(
        name="career_watchlist_update",
        summary="更新当前用户关注的公司、岗位关键词和城市；默认与现有列表合并。",
        input_schema=object_schema({
            "companies": {**_STRING_ARRAY, "description": "公司规范名或别名。"},
            "keywords": {**_STRING_ARRAY, "description": "岗位检索关键词。"},
            "locations": {**_STRING_ARRAY, "description": "可选城市过滤。"},
            "replace": {"type": "boolean", "description": "为 true 时替换传入的非空维度。", "default": False},
        }),
        output_schema=_PAYLOAD_SCHEMA,
        handler=_watchlist_update,
        category="career.intelligence",
        owner="career",
        module=__name__,
        metadata={"tags": ["career", "write"]},
    ),
    ToolDef(
        name="career_watchlist_show",
        summary="查看当前用户的 AI 岗位情报关注公司、关键词和城市。",
        input_schema=object_schema(), output_schema=_PAYLOAD_SCHEMA, handler=_watchlist_show,
        category="career.intelligence", owner="career", module=__name__,
    ),
    ToolDef(
        name="search_company_ai_jobs",
        summary=(
            "按用户指定公司查询公开招聘源，保存快照并返回新增、变化、疑似下线岗位，"
            "以及需要统一搜索入口执行的 fallback_query。"
        ),
        input_schema=object_schema({
            "companies": {**_STRING_ARRAY, "description": "留空使用 watchlist。"},
            "keywords": {**_STRING_ARRAY, "description": "留空使用 watchlist。"},
            "locations": {**_STRING_ARRAY, "description": "留空使用 watchlist。"},
            "posted_within_days": {"type": "integer", "description": "只保留最近多少天发布的岗位。", "default": 30},
            "limit_per_company": {"type": "integer", "description": "每家公司最多返回数，1-50。", "default": 20},
        }),
        output_schema=_PAYLOAD_SCHEMA, handler=_search_jobs,
        category="career.intelligence", owner="career", module=__name__, weight="heavy",
        metadata={"tags": ["career", "search", "write"]},
    ),
    ToolDef(
        name="career_jobs_ingest",
        summary=(
            "把统一搜索入口从用户指定公司的官方招聘链接找到的岗位写入快照。"
            "搜索摘要、社区帖子和面经链接不能作为岗位来源；fallback 快照不会判定岗位下线。"
        ),
        input_schema=object_schema({
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "title": {"type": "string"},
                        "location": {"type": "string"},
                        "source_url": {"type": "string"},
                        "source_job_id": {"type": "string"},
                        "responsibilities": {"type": "string"},
                        "requirements": {"type": "string"},
                        "published_at": {"type": "string", "description": "可解析的发布日期。"},
                    },
                    "required": ["company", "title", "source_url", "published_at"],
                },
            },
            "scan_scope": {
                "type": "object",
                "properties": {
                    "keywords": _STRING_ARRAY,
                    "locations": _STRING_ARRAY,
                    "posted_within_days": {"type": "integer", "default": 30},
                    "source_name": {"type": "string", "default": "search_information"},
                },
                "required": ["keywords", "posted_within_days", "source_name"],
            },
        }, required=("records", "scan_scope")),
        output_schema=_PAYLOAD_SCHEMA, handler=_ingest_jobs,
        category="career.intelligence", owner="career", module=__name__,
        metadata={"tags": ["career", "write"]},
    ),
    ToolDef(
        name="career_intel_ingest",
        summary=(
            "保存联网研究得到的薪资、待遇、面试流程或面试问题证据。每条必须包含公开链接、"
            "来源等级 A-D 和置信度；不得只写无法拆解的推测总包。"
        ),
        input_schema=object_schema({
            "records": {
                "type": "array",
                "description": "结构化证据数组。",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["salary", "benefits", "interview_process", "interview_question", "workplace"]},
                        "company": {"type": "string"},
                        "role_family": {"type": "string"},
                        "topic": {"type": "string"},
                        "normalized_key": {"type": "string", "description": "面试题归一化键；同题不同表述使用相同值。"},
                        "source_name": {"type": "string"},
                        "source_url": {"type": "string"},
                        "source_type": {"type": "string", "enum": ["official", "complete_experience", "community_post", "search_snippet", "repost"]},
                        "source_grade": {"type": "string", "enum": ["A", "B", "C", "D"]},
                        "published_at": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "payload": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["kind", "company", "source_name", "source_url", "source_type", "source_grade", "published_at", "confidence"],
                },
            }
        }, required=("records",)),
        output_schema=_PAYLOAD_SCHEMA, handler=_ingest,
        category="career.intelligence", owner="career", module=__name__,
        metadata={"tags": ["career", "write"]},
    ),
    ToolDef(
        name="career_intel_query",
        summary="查询已保存的岗位、市场证据、高频面试题和最近一次扫描状态。",
        input_schema=object_schema({
            "companies": {**_STRING_ARRAY, "description": "可选公司过滤。"},
            "kind": {"type": "string", "enum": ["all", "jobs", "evidence"], "default": "all"},
            "limit": {"type": "integer", "default": 50},
            "since_days": {"type": "integer", "description": "仅查询最近多少天，默认 365。", "default": 365},
            "role_family": {"type": "string", "description": "可选岗位族过滤。"},
            "detail": {"type": "boolean", "description": "是否返回完整 JD/证据细节。", "default": False},
        }),
        output_schema=_PAYLOAD_SCHEMA, handler=_query,
        category="career.intelligence", owner="career", module=__name__,
    ),
]

TOOL_PROVIDER = static_tool_provider(
    "career",
    packs={"career.intelligence": tuple(TOOLS)},
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
