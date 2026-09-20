"""Read-only repair flow projection from business receipts and explicitly bound archives."""
from __future__ import annotations

import json
import re
from typing import Any

from chatcopilot.harness.models import HarnessError

GROUPS = (("source", "来源与验收预期"), ("preparation", "复现准备"), ("baseline", "基线验证"),
          ("attempts", "修复尝试"), ("delivery", "交付结果"))


def _without_commands(value: Any) -> Any:
    def command(item: Any) -> bool:
        return isinstance(item, dict) and (item.get("type") == "command_execution" or item.get("kind") == "command_execution")
    if command(value):
        return None
    if isinstance(value, dict):
        return {key: _without_commands(item) for key, item in value.items()
                if not command(item)}
    if isinstance(value, (tuple, list)):
        return [_without_commands(item) for item in value if not command(item)]
    return value


def _brief(value: Any) -> str:
    if value is None or value == {}:
        return "输入未记录"
    labels = {"source": "来源", "checks": "检查项", "plan": "验证计划", "candidate_digest": "候选摘要",
              "diagnosis": "复现依据", "acceptance": "验收条件", "previous_failure": "上版失败",
              "hypothesis": "根因假设", "protected_cases": "保护项", "feedback": "修复提示"}
    if isinstance(value, dict):
        return "；".join(f"{labels.get(k, k)}：{json.dumps(v, ensure_ascii=False, default=str)[:180]}"
                         for k, v in value.items())[:500] or "输入未记录"
    return str(value)[:500]


def _outcome(value: dict[str, Any], phase: str) -> tuple[str, str]:
    if value.get("error"):
        error = value["error"]
        return "failed", str(error.get("message", error) if isinstance(error, dict) else error)
    if value.get("complete") is False or value.get("state") == "running":
        return "running", "执行中，尚无结论"
    if "decision" in value:
        return ("completed" if value["decision"] == "approved" else "failed",
                value.get("reason") or value.get("problem") or "审核结论已记录")
    failed, passed = value.get("failed_cases", []), value.get("passed_cases", [])
    if "passed_cases" in value or "failed_cases" in value:
        if phase.startswith("reproduce"):
            return "completed", "已复现原问题：目标检查出现预期失败" if failed else "当前版本未复现原问题"
        return ("failed" if failed and not phase.startswith(("baseline", "repository_baseline")) else "completed",
                f"通过 {len(passed)} 项，未通过 {len(failed)} 项")
    return "completed", "执行结果已记录，详见依据"


def flow_rows(task: dict[str, Any], attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in task.get("flow_steps", [])]
    locators = {json.dumps(row.get("locator", {}), sort_keys=True) for row in rows}

    def add(ident: str, title: str, parent: str, phase: str, value: Any, *,
            locator: dict[str, Any], inputs: Any = None, status: str | None = None,
            conclusion: str | None = None, source_id: str | None = None,
            revision: int | None = None, attempt: int | None = None,
            started_at: float | None = None, finished_at: float | None = None):
        if json.dumps(locator, sort_keys=True) in locators:
            return
        state, text = _outcome(value if isinstance(value, dict) else {}, phase)
        rows.append({"id": ident, "title": title, "parent_id": parent, "phase": phase,
                     "status": status or state, "conclusion": conclusion or text, "input": inputs,
                     "locator": locator, "source_id": source_id, "revision": revision, "attempt": attempt,
                     "started_at": started_at, "finished_at": finished_at, "historical": True})

    source = task.get("preparation_input") or task["source"]
    add("source-evidence", "来源与冻结证据", "source", "source", source, locator={"section": "source"},
        inputs={k: source[k] for k in ("kind", "run_id", "case_instance_id", "feedback", "original_input") if k in source},
        status="blocked" if source.get("blockers") else "completed",
        conclusion="来源存在阻塞，详见证据缺口" if source.get("blockers") else "来源已登记" + ("，存在证据缺口" if source.get("warnings") else ""))
    for rev in task.get("preparation_revisions", []):
        n = rev["revision"]
        for field, title in (("diagnosis", "生成复现方案"), ("trial", "复现方案试运行"), ("review", "复现方案审核")):
            if field not in rev:
                continue
            add(f"prepare-{n}-{field}", title, f"prepare-{n}", "prepare" if field == "diagnosis" else f"prepare_{field}", rev[field],
                locator={"section": "preparation", "revision": n, "field": field},
                inputs={"previous_failure": rev["reason"]} if field == "diagnosis" and rev.get("reason") else None,
                revision=n, attempt=rev.get("attempt"),
                source_id=f"prepare-review-{n}" if field == "review" else f"prepare-{n}" if field == "diagnosis" else None)
        if rev.get("status") != "running":
            add(f"prepare-{n}-result", "准备结果", f"prepare-{n}", "prepare_result", rev,
                locator={"section": "preparation", "revision": n}, revision=n, attempt=rev.get("attempt"),
                status="completed" if rev["status"] == "validated" else "failed",
                conclusion="复现方案校验通过" if rev["status"] == "validated" else rev.get("error", {}).get("message", "准备失败"),
                finished_at=rev.get("finished_at"))
    if task.get("verification_plan"):
        add("plan-current", "当前冻结验收方案", "preparation", "plan", {},
            locator={"section": "plans", "generation": task.get("plan_generation")},
            conclusion="已记录根因假设与冻结验收方案")
    for phase, value in task.get("evaluations", {}).items():
        match = re.fullmatch(r"(verify|confirm)-(\d+)(?:-r\d+)?", phase)
        n = int(match[2]) if match else None
        add("evaluation-" + phase, "独立确认" if phase.startswith("confirm") else "候选复测" if n else "确认原问题" if phase.startswith("reproduce") else "保护集基线",
            f"attempt-{n}" if n else "baseline", phase, value, locator={"section": "evaluations", "key": phase}, attempt=n)
    if task.get("regression_baseline") is not None:
        generation = task.get("plan_generation")
        add("repository-baseline", "仓库回归基线", "baseline", "repository_baseline", task["regression_baseline"],
            locator={"section": "regression_baseline", "generation": generation})
    for attempt in attempts:
        n = attempt["number"]
        if attempt.get("coding") is not None or attempt.get("error") or attempt.get("status") == "coding":
            add(f"coding-{n}", "生成候选", f"attempt-{n}", "coding", attempt.get("coding", {}),
                locator={"section": "attempts", "number": n, "field": "coding"}, attempt=n, source_id=f"coding-{n}",
                status="running" if attempt["status"] == "coding" else "failed" if attempt.get("error") else "completed",
                conclusion=attempt.get("error") or ("执行中，尚无结论" if attempt["status"] == "coding" else "候选执行已完成；验收结论见复测与审核"),
                started_at=attempt.get("started_at"))
        for field, title in (("verification", "候选复测"), ("confirmation", "独立确认")):
            receipt = attempt.get(field)
            if not receipt or (receipt.get("evaluation_id") and any(v.get("evaluation_id") == receipt["evaluation_id"] for v in task.get("evaluations", {}).values())):
                continue
            add(f"attempt-{n}-{field}", title, f"attempt-{n}", "verify" if field == "verification" else "confirm", receipt,
                locator={"section": "attempts", "number": n, "field": field}, attempt=n)
        for field, title in (("repository_regressions", "仓库回归"), ("review", "AI 修复审核")):
            if field in attempt:
                add(f"attempt-{n}-{field}", title, f"attempt-{n}", field, attempt[field],
                    locator={"section": "attempts", "number": n, "field": field}, attempt=n,
                    source_id=f"review-{n}" if field == "review" else None)
        if attempt.get("verification") is not None:
            add(f"attempt-{n}-result", "本轮验收结果", f"attempt-{n}", "acceptance", attempt,
                locator={"section": "attempts", "number": n}, attempt=n,
                status="completed" if attempt["status"] in {"accepted", "needs_review"} else "failed" if attempt["status"] in {"rejected", "review_rejected", "blocked"} else "running",
                conclusion="局部候选已验证；完整目标仍有缺口" if attempt["status"] == "needs_review" else "本轮验收通过" if attempt["status"] == "accepted" else "本轮验收未通过" if attempt["status"] in {"rejected", "review_rejected", "blocked"} else "等待审核或最终验收",
                finished_at=attempt.get("finished_at"))
    delivery = task.get("delivery") or {}
    for index, check in enumerate(task.get("commit_checks", [])):
        add(f"host-check-{index}", check["label"], "delivery", "host_check", {},
            locator={"section": "commit_checks", "index": index},
            status="completed" if check.get("exit_code") == 0 else "failed" if check.get("exit_code") is not None else "recorded",
            conclusion="宿主检查通过" if check.get("exit_code") == 0 else "宿主检查未通过" if check.get("exit_code") is not None else "退出状态未记录")
    if delivery.get("public_checks"):
        add("delivery-public-checks", "交付宿主检查", "delivery", "host_check", {},
            locator={"section": "delivery", "field": "public_checks"}, status="recorded", conclusion="交付检查回执已记录")
    for key, title in (("commit_sha", "提交候选"), ("pr_url", "创建 PR"), ("checks", "CI 检查"), ("merge_sha", "合并结果")):
        if delivery.get(key):
            value = delivery[key]
            conclusion = str(value) if isinstance(value, str) else "CI 结果已记录；各检查结论见依据"
            status = "completed"
            if key == "checks":
                status = "failed" if any(c.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"} for c in value) else "running" if any(c.get("status") not in {"completed", "COMPLETED"} and not c.get("conclusion") for c in value) else "completed"
            add("delivery-" + key, title, "delivery", key, {}, locator={"section": "delivery", "field": key},
                inputs={"repository": delivery.get("repository"), "base_branch": delivery.get("base_branch")},
                conclusion=conclusion, status=status)
    if task.get("local_commit") and not delivery:
        add("local-commit", "本地提交与回归收录", "delivery", "commit", {}, locator={"section": "local_commit"}, conclusion="本地提交已记录")
    if task.get("cleanup") and any(v not in {"pending", "not_created", None} for v in task["cleanup"].values()):
        add("cleanup", "归档与清理", "delivery", "cleanup", {}, locator={"section": "cleanup"},
            status="blocked" if task["cleanup"].get("error") else "completed", conclusion=task["cleanup"].get("error") or "清理回执已记录，详见各项状态")
    if task.get("status") not in {"queued", "running", "cancel_requested"}:
        for row in rows:
            if row["status"] == "running" and row["parent_id"] != "delivery":
                row.update(status="interrupted", conclusion="本步骤未记录可靠终态；任务已结束")
    return rows


def project_flow(task: dict[str, Any], attempts: list[dict[str, Any]]) -> dict[str, Any]:
    rows = flow_rows(task, attempts)
    groups = [{"id": key, "title": title, "parent_id": None} for key, title in GROUPS]
    for attempt in attempts:
        groups.append({"id": f"attempt-{attempt['number']}", "title": f"第 {attempt['number']} 轮", "parent_id": "attempts"})
    for rev in task.get("preparation_revisions", []):
        groups.append({"id": f"prepare-{rev['revision']}", "title": f"第 {rev['revision']} 版", "started_at": rev.get("started_at"), "parent_id": f"attempt-{rev['attempt']}" if rev.get("attempt") else "preparation"})
    # A running step can precede the corresponding result receipt in the task payload.
    for row in rows:
        parent = row["parent_id"]
        if not any(g["id"] == parent for g in groups):
            groups.append({"id": parent, "title": f"第 {row.get('revision') or row.get('attempt')} {'版' if parent.startswith('prepare') else '轮'}",
                           "parent_id": "preparation" if parent.startswith("prepare") else "attempts"})
    current = next((r["id"] for r in reversed(rows) if r["status"] == "running"), None)
    finished = max((r for r in rows if r.get("finished_at") or r.get("interrupted_at")),
                   key=lambda r: r.get("finished_at") or r["interrupted_at"], default=None)
    return {"task_id": task["task_id"], "groups": groups, "current_step_id": current,
            "default_step_id": current or (finished or rows[-1])["id"] if rows else None,
            "steps": [{**{k: r.get(k) for k in ("id", "title", "parent_id", "phase", "status", "started_at", "finished_at", "attempt", "revision", "generation", "source_id", "input_truncated")},
                       "input_summary": _brief(r.get("input")), "conclusion": str(r["conclusion"])[:800]} for r in rows]}


def step_detail(task: dict[str, Any], attempts: list[dict[str, Any]], step_id: str) -> dict[str, Any]:
    row = next((r for r in flow_rows(task, attempts) if r["id"] == step_id), None)
    if row is None:
        raise HarnessError("not_found", "此修复任务没有该流程步骤")
    loc = row["locator"]
    section = loc.get("section")
    value: Any = None
    if section == "source":
        value = {"source": task.get("preparation_input") or task["source"], "acceptance": task.get("acceptance")}
    elif section == "preparation":
        value = next((r for r in task.get("preparation_revisions", []) if r["revision"] == loc["revision"]), {})
        if loc.get("field"):
            value = value.get(loc["field"])
    elif section == "attempts":
        value = next((a for a in attempts if a["number"] == loc["number"]), {})
        if loc.get("field"):
            value = value.get(loc["field"])
    elif section == "evaluations":
        value = task.get("evaluations", {}).get(loc["key"])
        if value and value.get("flow_step_id") and value["flow_step_id"] != step_id and not row.get("historical"):
            value = task.get("evaluation_history", {}).get(step_id)
    elif section == "regression_baseline":
        value = task.get("regression_baseline") if loc.get("generation") == task.get("plan_generation") else task.get("regression_baseline_versions", {}).get(str(loc.get("generation")))
    elif section in {"delivery", "cleanup", "local_commit"}:
        value = task.get(section)
        if loc.get("field"):
            value = (value or {}).get(loc["field"])
    elif section in {"plans", "workspace"}:
        value = row.get("evidence")
        if section == "plans" and row.get("historical") and loc.get("generation") == task.get("plan_generation"):
            value = {"hypothesis": task.get("hypothesis"), "plan": task.get("verification_plan")}
    elif section == "commit_checks":
        check = task["commit_checks"][loc["index"]]
        value = {"label": check["label"], "exit_code": check.get("exit_code"),
                 "output_reference": f"原始资料 → 完整修复记录 → commit_checks[{loc['index']}].output"}
    if "receipt" in row:
        value = row["receipt"]
    traces = []
    expected = None
    if section == "preparation" and loc.get("field") in {"diagnosis", "review"}:
        expected = f"reproducer/revision-{loc['revision']}/" + ("review/" if loc["field"] == "review" else "") + "traces"
    elif section == "attempts" and loc.get("field") in {"coding", "review"}:
        expected = f"attempt-{loc['number']}/" + ("review/" if loc["field"] == "review" else "") + "traces"
    for record in task.get("trace_records", {}).values():
        binding = record.get("source", {}).get("flow_step_id")
        if binding == step_id or (not binding and expected and record.get("directory") == expected):
            traces.append({k: v for k, v in record.items() if k != "directory"})
    return {"id": step_id, "attempt": row.get("attempt"), "status": row["status"], "input": row.get("input"),
            "input_truncated": row.get("input_truncated", False), "conclusion": row["conclusion"],
            "evidence": row.get("evidence"), "result": _without_commands(value), "traces": traces,
            "source_id": row.get("source_id"), "detail_state": "available" if value is not None else "pending" if row["status"] == "running" else "not_recorded"}
