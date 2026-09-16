"""Repository maintenance vocabulary; executable checks keep their own authority."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.health_policy import scan_path

RULES = (
    {"id": "architecture", "title": "遵守依赖与职责边界", "detector": "architecture",
     "reference": "specs/runtime-four-layer-definition/spec.md", "guidance": "使用已有公开契约，不跨域访问私有实现。"},
    {"id": "hygiene", "title": "清理明确的代码错误与冗余", "detector": "ruff",
     "reference": "pyproject.toml", "guidance": "按当前 Ruff 规则处理，不能增加忽略或降低检查强度。"},
    {"id": "duplication", "title": "复用相同业务契约的实现", "detector": "codex",
     "reference": "docs/reference/agent.md", "guidance": "追踪调用方和语义差异；相似代码本身不证明可合并。"},
    {"id": "boundary", "title": "在实际边界确认数据契约", "detector": "codex",
     "reference": "docs/reference/runtime.md", "guidance": "沿来源追踪解析和授权；不按 get/hasattr 关键词认定错误。"},
    {"id": "obsolete", "title": "以调用依据清理过时实现", "detector": "codex",
     "reference": "AGENTS.md", "guidance": "核对动态注册、公开导出和配置消费；兼容影响不明须等待判断。"},
    {"id": "documentation", "title": "文档反映当前事实", "detector": "docs/codex",
     "reference": "docs/maintenance.md", "guidance": "按索引、领域正文、源码入口读取；核对具体冲突，不凭日期判过时。普通文档可修复，规范只报告；不写运行流水。"},
)
SCOPES = {
    "all": ("src", "console", "scripts", "docs"),
    "runtime": ("src",),
    "console": ("console",),
    "docs": ("docs",),
}


def in_scope(path: str, scope: str) -> bool:
    if scope not in SCOPES:
        raise ValueError("未知扫描范围")
    return scan_path(path, scope)


def finding(rule_id: str, path: str, line: int, summary: str, evidence: str,
            recommendation: str, *, detector: str, disposition: str = "candidate",
            group_key: str = "", related_paths: list[str] | None = None,
            depends_on: list[str] | None = None, change_kind: str = "bugfix") -> dict[str, Any]:
    identity = [rule_id, path, summary, evidence]
    return {"id": hashlib.sha256(json_text(identity).encode()).hexdigest()[:20],
            "rule_id": rule_id, "path": path, "line": line, "summary": summary,
            "evidence": evidence, "recommendation": recommendation,
            "detector": detector, "disposition": disposition,
            "group_key": group_key or f"{rule_id}:{path}", "related_paths": related_paths or [path],
            "depends_on": depends_on or [], "change_kind": change_kind}


def parse_audit(value: Any, root: Path, scope: str, *, delivered_paths: tuple[str, ...] = ()) -> dict[str, Any]:
    """Validate model output against source files; it never defines permissions."""
    if not isinstance(value, dict) or set(value) != {"findings", "inspected_paths", "summary"}:
        raise HarnessError("audit_invalid", "巡检结果缺少问题清单、阅读文件或摘要")
    if not isinstance(value["findings"], list) or not isinstance(value["inspected_paths"], list):
        raise HarnessError("audit_invalid", "巡检结果清单无效")
    if not isinstance(value["summary"], str):
        raise HarnessError("audit_invalid", "巡检摘要无效")

    def source_path(raw: Any, *, finding_scope: bool = True) -> Path:
        if not isinstance(raw, str) or not raw or "\\" in raw:
            raise HarnessError("audit_invalid", "巡检必须引用仓库相对文件")
        path = Path(raw)
        target = root / path
        if path.is_absolute() or ".." in path.parts or (finding_scope and not in_scope(raw, scope)):
            raise HarnessError("audit_invalid", "巡检引用超出扫描范围")
        if target.resolve() != target or not target.is_file():
            raise HarnessError("audit_invalid", "巡检引用的源码不存在或不是普通文件")
        return target

    inspected = list(dict.fromkeys(value["inspected_paths"]))
    for name in inspected:
        source_path(name, finding_scope=False)
    if not delivered_paths and not any(in_scope(name, scope) for name in inspected):
        raise HarnessError("audit_incomplete", "巡检没有读取任何范围内源码，无法确认巡检结果")
    rows = []
    for item in value["findings"]:
        required = {
            "rule_id", "path", "line", "summary", "evidence", "recommendation", "disposition"
        }
        optional = {"group_key", "related_paths", "depends_on", "change_kind"}
        if not isinstance(item, dict) or not required.issubset(item) or set(item) - required - optional:
            raise HarnessError("audit_invalid", "巡检问题字段无效")
        if item["rule_id"] not in {r["id"] for r in RULES}:
            raise HarnessError("audit_invalid", "巡检引用了未知规则")
        target = source_path(item["path"])
        if (type(item["line"]) is not int or item["line"] < 1
                or item["line"] > max(1, len(target.read_bytes().splitlines()))):
            raise HarnessError("audit_invalid", "巡检行号无效")
        if any(not isinstance(item[k], str) or not item[k].strip()
               for k in ("summary", "evidence", "recommendation")):
            raise HarnessError("audit_invalid", "巡检问题没有完整证据和修复建议")
        if item["disposition"] not in {"candidate", "needs_decision"}:
            raise HarnessError("audit_invalid", "巡检处置类型无效")
        if "group_key" in item and (not isinstance(item["group_key"], str) or not item["group_key"].strip()):
            raise HarnessError("audit_invalid", "根因分组标识无效")
        if item.get("change_kind", "bugfix") not in {"bugfix", "refactor"}:
            raise HarnessError("audit_invalid", "变更类型无效")
        for key in ("related_paths", "depends_on"):
            if key in item and (not isinstance(item[key], list) or any(not isinstance(x, str) for x in item[key])):
                raise HarnessError("audit_invalid", "关联路径或依赖格式无效")
        for name in item.get("related_paths", []):
            source_path(name, finding_scope=False)
        rows.append(finding(**item, detector="codex"))
    return {"findings": rows, "inspected_paths": inspected, "summary": value["summary"]}
