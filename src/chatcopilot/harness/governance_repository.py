"""Frozen repository navigation and independently checkable GC evidence."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import re
from pathlib import Path

import yaml

from chatcopilot.core.private_sqlite import json_text
from chatcopilot.harness.models import HarnessError
from chatcopilot.harness.verification_policy import governance_policy_path
from chatcopilot.harness.skill_context import LESSONS_PATH


GOAL = "依据现行 SDD 和黄金原则自主调查全仓，选择一个有证据的代码熵问题，消除偏离并保持原有行为。"


def rule_path(reference: str) -> str:
    return re.sub(r":\d+(?:-\d+)?$", "", reference.split("#", 1)[0])


def freeze_context(artifacts, source: Path, manifest: dict, editable: list[str], principles: dict, *,
                   learning: bool = False):
    rules = {row["path"]: row for row in principles["documents"]}
    for name in sorted(manifest):
        if not (name.startswith("specs/") and name.endswith("/spec.md")):
            continue
        content = (source / name).read_text()
        parts = content.split("---", 2)
        metadata = yaml.safe_load(parts[1]) if len(parts) == 3 else {}
        if isinstance(metadata, dict) and metadata.get("status") in {"accepted", "implemented"}:
            rules[name] = {"path": name, "sha256": manifest[name]["sha256"], "content": content}
    return artifacts.put("governance_context", 1, {
        "files": manifest, "editable_paths": editable, "rules": list(rules.values()),
        "skill_learning": learning,
        "reading": "先阅读规则索引，自主追踪调用方；范围清单不等于已阅读，不要求按文件切片。",
    })


def _relative(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and path.as_posix() == name


def validate_report(value: dict, context: dict, baseline: Path) -> dict:
    files = context["files"]
    reported = {Path(name).as_posix() for name in value["inspected_paths"]}
    inspected = reported & set(files)
    uninspected = [*value["uninspected"], *("未逐文件核实的报告路径：" + name for name in sorted(reported - set(files)))]
    ids = [finding["id"] for finding in value["findings"]]
    if len(ids) > 1:
        raise HarnessError("invalid_role_result", "每项任务最多报告一个代码熵问题，发现首个问题后应立即停止调查")
    if len(ids) != len(set(ids)) or any(not ident.strip() for ident in ids):
        raise HarnessError("invalid_role_result", "熵回收发现标识为空或重复")
    rules = {row["path"] for row in context["rules"]}
    receipts = []
    findings = []
    for finding in value["findings"]:
        if (not finding["principle_refs"] or not finding["acceptance_criteria"]
                or not finding["affected_paths"] or not finding["summary"].strip()
                or not finding["impact"].strip()):
            raise HarnessError("invalid_role_result", "熵回收发现缺少规则、影响、修改范围或验收依据")
        if any(rule_path(ref) not in rules for ref in finding["principle_refs"]):
            raise HarnessError("invalid_role_result", "熵回收依据必须引用冻结的黄金原则或现行 SDD")
        if any(not _relative(path) for path in finding["affected_paths"]):
            raise HarnessError("invalid_role_result", "熵回收修改范围必须为仓库相对路径")
        if context.get("skill_learning") and set(finding["affected_paths"]) != {LESSONS_PATH}:
            raise HarnessError("invalid_role_result", "Skill 学习任务只能修改指定参考文件")
        if any(path.startswith("tests/") and path not in files for path in finding["affected_paths"]):
            raise HarnessError("invalid_role_result", "Test 草案由独立角色和宿主收录，不能列为熵回收产品改动")
        extracts = []
        for evidence in finding["evidence"]:
            name = Path(evidence["path"]).as_posix()
            if name not in files:
                raise HarnessError("invalid_role_result", "熵回收源码引用不在冻结仓库中：" + name)
            inspected.add(name)
            content = (baseline / name).read_text(errors="replace")
            lines = content.splitlines(keepends=True)
            start, end = evidence["start_line"], evidence["end_line"]
            if not 1 <= start <= end <= len(lines):
                raise HarnessError("invalid_role_result", f"熵回收源码引用 {name}:{start}-{end} 超出冻结基线（共 {len(lines)} 行）")
            excerpt = "".join(lines[start - 1:end])
            extracts.append({**evidence, "path": name, "excerpt": excerpt})
            receipts.append({"finding_id": finding["id"], "path": name,
                "sha256": files[name]["sha256"], "line": start, "end_line": end})
        # New product files are checked again by the existing candidate boundary.
        sensitive = any((governance_policy_path(path) and not (context.get("skill_learning") and path == LESSONS_PATH))
                        or path in files and path not in context["editable_paths"]
                        for path in finding["affected_paths"])
        findings.append({**finding, "evidence": extracts, "disposition": "needs_decision" if sensitive else finding["disposition"]})
    selected = next((item for item in findings if item["id"] == value["selected_finding_id"]), None)
    decision = value["decision"]
    if decision == "proceed" and selected is None:
        raise HarnessError("invalid_role_result", "继续熵回收必须选择一个明确主题")
    if selected and selected["disposition"] == "needs_decision":
        decision = "needs_review"
    if decision == "no_changes" and any(item["disposition"] == "automatic" for item in findings):
        raise HarnessError("invalid_role_result", "存在可执行发现，不能声明无可执行改动")
    if decision == "no_changes" and findings:
        decision = "needs_review"
    if decision == "needs_review" and not findings and not value["unresolved"]:
        raise HarnessError("invalid_role_result", "待判断结论必须说明具体事项")
    return {**value, "decision": decision, "findings": findings, "selected": selected, "inspected_paths": sorted(inspected),
            "uninspected": uninspected,
            "evidence_receipts": receipts, "inventory_count": len(files),
            "inspection_complete": {row["path"] for row in receipts} == set(files) and not uninspected}


def bind_report(store, task_id: str, artifacts, value: dict, baseline: Path) -> dict:
    task = store.get(task_id)
    context = artifacts.read(task["governance_context"])
    report = validate_report(value, context, baseline)
    finding = report["findings"][0] if report["findings"] else None
    if task.get("frozen_finding"):
        frozen = artifacts.read(task["frozen_finding"])
        if not finding or any(finding[key] != frozen[key] for key in (
                "id", "principle_refs", "evidence", "affected_paths", "acceptance_criteria")):
            raise HarnessError("governance_target_changed", "返工不能更换或扩大已冻结的问题，请围绕原问题修复")
        report["findings"] = [frozen]
        report["selected"] = frozen if report["selected"] else None
    elif finding:
        reference = artifacts.put("frozen_finding", 1, finding)
        store.update(task_id, frozen_finding=asdict(reference), governance_finding_id=finding["id"])
    reference = artifacts.put("governance_report", task["current_attempt"], report)
    source = {**task["source"], "governance_target": report["selected"],
              "governance_report": asdict(reference)}
    store.update(task_id, governance_report=asdict(reference), source=source,
        governance_summary={"summary": report["summary"], "topic": (report["selected"] or {}).get("summary"),
            "inspected_count": len(set(report["inspected_paths"])), "inventory_count": report["inventory_count"],
            "inspection_complete": report["inspection_complete"], "finding_count": len(report["findings"]),
            "needs_decision_count": sum(f["disposition"] == "needs_decision" for f in report["findings"])})
    return report


def require_improvement(target: dict, review: dict, baseline: Path, candidate: Path, changed: list[str]) -> dict:
    if (review.get("finding_id") != target["id"] or not review.get("behavior_preserved")
            or not review.get("improvements")):
        raise HarnessError("review_inconclusive", "熵回收审核缺少目标对应的改善与行为保持证据")
    if not set(changed).issubset(target["affected_paths"]):
        raise HarnessError("needs_replan", "候选修改超出已冻结熵回收主题的文件范围")
    evidence = []
    for row in review["improvements"]:
        path = row["path"]
        if not _relative(path) or path not in changed or not row["reason"].strip():
            raise HarnessError("review_inconclusive", "改善证据必须指向本次实际变更")
        before, after = baseline / path, candidate / path
        old = before.read_text(errors="replace") if before.is_file() else ""
        new = after.read_text(errors="replace") if after.is_file() else ""
        if old == new or row["before"] == row["after"]:
            raise HarnessError("review_inconclusive", "改善证据没有前后变化")
        if (row["before"] and row["before"] not in old) or (row["after"] and row["after"] not in new):
            raise HarnessError("review_inconclusive", "改善片段与冻结基线或候选不匹配")
        if not row["before"] and before.exists():
            raise HarnessError("review_inconclusive", "已有文件不能使用空的基线证据")
        if not row["after"] and row["before"] in new:
            raise HarnessError("review_inconclusive", "声明已移除的内容仍存在")
        evidence.append({**row, "before_sha256": hashlib.sha256(old.encode()).hexdigest(),
                         "after_sha256": hashlib.sha256(new.encode()).hexdigest()})
    return {"finding_id": target["id"], "behavior_preserved": True, "improvements": evidence,
            "target_digest": hashlib.sha256(json_text(target).encode()).hexdigest()}
