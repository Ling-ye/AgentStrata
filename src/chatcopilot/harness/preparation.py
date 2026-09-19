"""Host-owned acceptance requirements and review of executable draft boundaries."""
from __future__ import annotations

import ast
import hashlib
from typing import Any

from chatcopilot.harness.models import HarnessError


def acceptance(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("kind") == "code_health":
        from chatcopilot.core.private_sqlite import json_text
        target = source.get("governance_target")
        expected = ("；".join(target["acceptance_criteria"]) if target else source.get("original_input", ""))
        return {"original": expected, "sha256": hashlib.sha256(json_text({
                    "purpose": "governance", "target": target, "goal": expected}).encode()).hexdigest(),
                "purpose": "governance", "requires_image": False,
                "items": [{"id": "expected_behavior", "text": expected, "required": True, "verification": "behavior"}]}
    feedback = source.get("feedback", {})
    expected = (feedback.get("expected_behavior") or source.get("case_definition", {}).get("expected_behavior")
                or source.get("original_input") or "")
    # Input resources are facts. Output delivery is a separate, reviewed goal capability.
    image = bool(source.get("image_resources")) or bool(source.get("requires_image"))
    items = [{"id": "expected_behavior", "text": expected, "required": True,
              "verification": "agent" if image else "behavior"}]
    if image:
        items.extend({"id": name, "text": label, "required": True, "verification": kind}
                     for name, label, kind in (
                         ("input_image_materialized", "输入图片可获取并物化", "pytest"),
                         ("image_dispatched", "原图实际传入视觉后端", "agent")))
    if "image_delivery" in source.get("goal_capabilities", []):
        items.extend({"id": name, "text": label, "required": True, "verification": kind}
                     for name, label, kind in (
                         ("image_materialized", "候选图片下载校验并物化", "pytest"),
                         ("image_delivered", "真实会话发送链路产生图片与完整回执", "pytest")))
    return {"original": expected, "sha256": hashlib.sha256(expected.encode()).hexdigest(),
            "requires_image": image, "items": items}


def verification_purpose(source: dict[str, Any]) -> str:
    return "governance" if source.get("kind") == "code_health" else "repair"


def require_purpose(source: dict[str, Any], plan) -> None:
    if plan.purpose != verification_purpose(source):
        raise HarnessError("verification_purpose", "验收用途与宿主冻结来源不一致")


def review_test(content: bytes) -> None:
    """Reject concrete mechanisms that manufacture a missing API or launder errors."""
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if any(k.arg == "create" and isinstance(k.value, ast.Constant) and k.value.value is True
                   for k in node.keywords):
                raise HarnessError("test_definition", "测试使用 create=True 虚构接口；请隔离已有外部依赖并断言公开可观察行为")
        if isinstance(node, ast.ExceptHandler):
            names = {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)} if node.type else set()
            if node.type is None or names & {"Exception", "BaseException"}:
                if any(isinstance(n, ast.Assert) or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                                    and n.func.attr == "fail") for stmt in node.body for n in ast.walk(stmt)):
                    raise HarnessError("test_definition", "不能把任意异常改写为行为断言；请保留具体异常类型和原因码，并验证对照输入")


def classify(row: dict[str, Any]) -> str:
    if row.get("outcome") == "passed":
        return ""
    if any(p["outcome"] == "failed" and p["when"] != "call" for p in row.get("phases", [])):
        return "test_definition"
    chain = row.get("exception_chain", [])
    if any(e["type"] in {"PermissionError", "FileNotFoundError", "ConnectionError", "TimeoutError", "OSError"}
           for e in chain):
        return "environment"
    if row.get("assertion_failure"):
        return "product"
    if any(e.get("code") for e in chain):
        return "domain_exception"
    return "test_definition"


def require_coverage(requirements: dict[str, Any], diagnosis: dict[str, Any], *, local: bool, agent: bool) -> dict[str, list[str]]:
    coverage = diagnosis.get("coverage", {})
    if not isinstance(coverage, dict):
        raise HarnessError("acceptance_gap", "验收覆盖必须映射到实际检查")
    for item in requirements["items"]:
        kind = item["verification"]
        # A deterministic expectation may be established by a real product test;
        # image semantics always needs the Agent lane, irrespective of draft prose.
        needs_agent = requirements["requires_image"] and kind == "agent"
        if item["id"] not in coverage or not coverage[item["id"]] or (needs_agent and not agent):
            raise HarnessError("acceptance_gap", "草案遗漏必需验收项：" + item["id"])
        if kind == "pytest" and not local:
            raise HarnessError("acceptance_gap", "草案缺少确定性物化检查")
    return coverage
