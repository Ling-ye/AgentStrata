"""Task coordination. Normal transitions do not require another Main model turn."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from chatcopilot.harness.agent_types import AgentCall, AgentRunner, Role, retry_role, role_result
from chatcopilot.harness.artifact_repository import ArtifactRepository
from chatcopilot.harness.flow_records import record_step
from chatcopilot.harness.models import HarnessError, RepairHypothesis
from chatcopilot.harness.context_briefs import build_failure_brief, build_source_index, build_target_context


class RoleWorkflow:
    def __init__(self, store, task_id: str, runner: AgentRunner, artifacts: ArtifactRepository):
        self.store, self.task_id, self.runner, self.artifacts = store, task_id, runner, artifacts

    def _read(self, key: str) -> Any:
        ref = self.store.get(self.task_id).get("role_artifacts", {}).get(key)
        return self.artifacts.read(ref) if ref else None

    def call(self, role, root, number, goal, evidence, options, cancel):
        task = self.store.get(self.task_id)
        refs = task.get("role_artifacts", {})
        governance = task["source"].get("kind") == "code_health"
        relevant = {
            Role.MAIN: ("plan",), Role.PLAN: ("main", "plan"), Role.CODING: ("plan", "test", "coding"),
            Role.TEST: ("plan", "test"), Role.REVIEW: ("plan", "coding", "test", "review"),
        }[role]
        context = {**evidence, "base_commit": task["base_commit"],
                   "inputs": {key: self.artifacts.navigation(ref) for key, ref in refs.items() if key in relevant}}
        if role == Role.MAIN and context.get("source_index"):
            index = context["source_index"]
            context["source_index"] = {"attempt": index["attempt"], "seeds": index["seeds"],
                "candidate_count": len(index["candidates"]), "omitted_counts": index["omitted_counts"],
                "limits": index["limits"], "ref": context.get("source_index_ref")}
        elif role not in {Role.MAIN, Role.PLAN}:
            context.pop("source_index", None)
            context.pop("source_index_ref", None)
        if not governance or role == Role.PLAN:
            context["principles"] = self.artifacts.navigation(task["principles"])
        if not governance or role in {Role.MAIN, Role.PLAN, Role.TEST}:
            context["original_source"] = self.artifacts.navigation(task["problem_ref"])
        if governance and role == Role.PLAN:
            context["governance_context"] = self.artifacts.navigation(task["governance_context"])
        elif governance and task.get("target_context_ref"):
            context.setdefault("target_context", self.artifacts.read(task["target_context_ref"]))
            context["target_context_ref"] = self.artifacts.navigation(task["target_context_ref"])
        output = self.artifacts.directory / f"attempt-{number}" / role.value
        source_id = f"{role.value}-{number}"
        self.store.update(self.task_id, stage=role.value, current_role=role.value)
        with record_step(self.store, self.task_id, role.value, {
                Role.MAIN: "主 Agent 安排任务", Role.PLAN: "根因与修复计划", Role.CODING: "实现候选",
                Role.TEST: "建立独立验证", Role.REVIEW: "独立审核"}[role],
                group=f"attempt-{number}", attempt=number, source_id=source_id,
                locator={"section": "attempts", "number": number, "field": "review"}
                        if role == Role.REVIEW else None,
                inputs={"role": role.value, "goal": task["acceptance"], "assignment": goal, "inputs": refs,
                        "problem": task["problem_ref"], "principles": task["principles"]}) as step:
            cancel()
            result = self.runner.execute(root, AgentCall(self.task_id, role, number, goal, context), options(), output, cancel)
            cancel()
            value = role_result(role, result.payload, governance=governance)
            reference = self.artifacts.put(role.value, number, value)
            execution = self.artifacts.put("execution", number, result.execution)
            refs = {**self.store.get(self.task_id).get("role_artifacts", {}), role.value: asdict(reference)}
            self.store.update(self.task_id, role_artifacts=refs, current_role=None)
            step.conclusion = value.get("summary", value.get("reason", ""))
            step.evidence = {"role": role.value, "output": asdict(reference), "execution": asdict(execution)}
            return value

    def prepare_round(self, root: Path, baseline: Path, number: int, previous: dict | None,
                      options, cancel, capabilities: dict) -> tuple[dict, Path | None]:
        task = self.store.get(self.task_id)
        original = self.artifacts.read(task["problem_ref"])
        governance = original.get("kind") == "code_health"
        plan = self._read("plan")
        route = retry_role(previous["stage"], previous["code"]) if previous else Role.MAIN
        if not plan:
            route = Role.MAIN
        failure = None
        if previous and previous.get("brief_ref"):
            failure = self.artifacts.read(previous["brief_ref"])
        elif previous:
            failure = build_failure_brief(attempt={"number": number - 1, "feedback": previous},
                stage=previous.get("stage", "plan"), code=previous.get("code", "needs_replan"),
                message=previous.get("message", "上一轮未完成"), signature=previous.get("signature", ""))
        principles = self.artifacts.read(task["principles"])
        rules = principles.get("documents", [])
        if governance:
            rules = self.artifacts.read(task["governance_context"])["rules"]
        index = None
        index_ref = None
        if route in {Role.MAIN, Role.PLAN}:
            index = build_source_index(baseline=baseline, manifest=task["baseline_manifest"], source=original,
                                       rules=rules, attempt=number, failure=failure)
            index_ref = self.artifacts.put("source_index", number, index)
            self.store.update(self.task_id, source_index_ref=asdict(index_ref))
        evidence = {"source": {key: original[key] for key in ("kind", "bot_id", "trace_archive", "trace") if key in original},
                    "acceptance": task["acceptance"], "baseline_root": str(baseline),
                    "verification_capabilities": capabilities}
        if governance:
            if task.get("frozen_finding"):
                evidence["frozen_finding"] = self.artifacts.read(task["frozen_finding"])
        if failure:
            evidence.update(failure_brief=failure, failure_brief_ref=previous.get("brief_ref") if previous else None)
        if index_ref:
            evidence.update(source_index=index, source_index_ref=asdict(index_ref))
        if route == Role.MAIN:
            allowed = ["plan"] if not plan else ["plan", "coding", "test"]
            action = self.call(Role.MAIN, root, number, "安排下一项工作", {**evidence, "allowed_roles": allowed}, options, cancel)
            if action["next_role"] == "blocked":
                raise HarnessError("material_missing", "；".join(action["unresolved"]))
            if action["next_role"] not in allowed:
                raise HarnessError("invalid_role_result", "主 Agent 选择了当前不允许的步骤")
            route = Role(action["next_role"])
        if route == Role.PLAN:
            plan = self.call(Role.PLAN, root, number, "自主调查仓库并选择一个代码熵问题" if governance else "定位根因并提出最小修复", evidence, options, cancel)
            if plan["decision"] == "blocked" and not governance:
                raise HarnessError("plan_blocked", "；".join(plan["unresolved"]))
        if governance:
            from chatcopilot.harness.governance_repository import bind_report
            try:
                report = bind_report(self.store, self.task_id, self.artifacts, plan, baseline)
            except HarnessError:
                refs = dict(self.store.get(self.task_id).get("role_artifacts", {}))
                refs.pop("plan", None)
                self.store.update(self.task_id, role_artifacts=refs)
                raise
            if report["decision"] == "blocked":
                raise HarnessError("plan_blocked", "；".join(plan["unresolved"]))
            if report["decision"] in {"no_changes", "needs_review"}:
                return {"decision": report["decision"], "summary": report["summary"], "goal_capabilities": [],
                        "coverage": [], "gaps": [], "notes": []}, None
            original = {**original, "governance_target": report["selected"],
                        "governance_report": self.store.get(self.task_id)["governance_report"]}
            evidence["governance_target"] = report["selected"]
            target = build_target_context(report["selected"], rules)
            target_ref = self.artifacts.put("target_context", number, target)
            self.store.update(self.task_id, target_context_ref=asdict(target_ref))
            evidence["target_context"] = target
        evidence["repair_plan"] = plan
        goal_capabilities = plan["goal_capabilities"]
        from chatcopilot.harness.preparation import acceptance
        requirements = acceptance({**original, "goal_capabilities": goal_capabilities})
        self.store.update(self.task_id, acceptance=requirements, goal_capabilities=goal_capabilities,
                          hypothesis=asdict(RepairHypothesis(plan["summary"], requirements["original"], tuple(plan["evidence_refs"]))))
        evidence["acceptance"] = requirements
        existing = original.get("kind", "evaluation") == "evaluation" or governance and plan["verification_order"] == "existing"
        test = self._read("test")
        needs_test = not existing and (not test or route in {Role.TEST, Role.PLAN})
        coding = self._read("coding") or {"gaps": []}
        test_error = None

        def test_call():
            return self.call(Role.TEST, root, number, "建立原目标的独立复现与回归依据", evidence, options, cancel)

        if needs_test and plan["verification_order"] == "test_first":
            try:
                test = test_call()
            except HarnessError as exc:
                if exc.code not in {"invalid_role_result", "test_definition"}:
                    raise
                test_error = exc
        if route != Role.TEST or not self._read("coding"):
            coding = self.call(Role.CODING, root, number, "实现最小候选并报告局部检查", evidence, options, cancel)
            if coding["needs_replan"]:
                raise HarnessError("needs_replan", coding["summary"])
        if needs_test and plan["verification_order"] != "test_first":
            test = test_call()
        if test_error:
            raise test_error
        if existing:
            test = {"decision": "candidate", "summary": "复用冻结测评", "verification_kind": "existing",
                    "goal_capabilities": goal_capabilities,
                    "coverage": [{"requirement": "expected_behavior", "checks": ["repository_regressions" if governance else original["case_id"]]}], "gaps": [], "notes": []}
        proposal = {**test, "goal_capabilities": goal_capabilities, "gaps": [*test["gaps"], *coding["gaps"]]}
        if any(g["requirement"] not in {item["id"] for item in requirements["items"]} for g in proposal["gaps"]):
            raise HarnessError("test_definition", "角色缺口必须绑定本任务的实际验收目标")
        reference = self.store.get(self.task_id).get("role_artifacts", {}).get("test")
        draft = self.artifacts.directory / f"attempt-{reference['revision']}" / "test" / "draft" if reference and not existing else None
        return proposal, draft

    def review(self, root, evidence, options, output, cancel):
        task = self.store.get(self.task_id)
        value = self.call(Role.REVIEW, root, task["current_attempt"], "独立审查候选和验收证据", evidence,
                          lambda: options, cancel)
        return {**value, "execution": {}}
